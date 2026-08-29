package connection

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"h3studio/cli/internal/config"
	"h3studio/cli/internal/contract"
)

type Process interface {
	Wait() error
	Stop(context.Context) error
	Exited() bool
}

type Starter func(name string, args []string, stdin io.Reader, stderr io.Writer) (Process, error)
type ControlRunner func(context.Context, string, []string) error
type Probe func(context.Context, string) error
type PortAllocator func() (int, error)
type TempDir func() (string, error)

type Options struct {
	Executable      string
	StartupTimeout  time.Duration
	CleanupTimeout  time.Duration
	ControlTimeout  time.Duration
	ForwardAttempts int
	NonInteractive  bool
	Stderr          io.Writer
	Start           Starter
	RunControl      ControlRunner
	Probe           Probe
	AllocatePort    PortAllocator
	CreateTempDir   TempDir
	RemoveAll       func(string) error
}

type supervisor struct {
	process Process
	done    chan struct{}
	mu      sync.Mutex
	waitErr error
}

func supervise(process Process) *supervisor {
	value := &supervisor{process: process, done: make(chan struct{})}
	go func() {
		err := process.Wait()
		value.mu.Lock()
		value.waitErr = err
		value.mu.Unlock()
		close(value.done)
	}()
	return value
}

func (s *supervisor) exited() (bool, error) {
	if s.process.Exited() {
		select {
		case <-s.done:
			s.mu.Lock()
			defer s.mu.Unlock()
			return true, s.waitErr
		default:
			return true, nil
		}
	}
	select {
	case <-s.done:
		s.mu.Lock()
		defer s.mu.Unlock()
		return true, s.waitErr
	default:
		return false, nil
	}
}

type Session struct {
	BaseURL        string
	supervisor     *supervisor
	cleanup        time.Duration
	controlTimeout time.Duration
	executable     string
	target         string
	sshPort        int
	controlPath    string
	controlReady   bool
	tempDir        string
	runControl     ControlRunner
	removeAll      func(string) error
	closeOnce      sync.Once
	closeComplete  chan struct{}
	closeErr       error
}

func Open(ctx context.Context, value config.Context, options Options) (*Session, error) {
	normalized, err := config.NormalizeContext(value)
	if err != nil {
		return nil, &contract.CLIError{Code: "invalid_context", Message: err.Error(), Cause: err}
	}
	if normalized.Server != "" {
		return &Session{BaseURL: normalized.Server, closeComplete: make(chan struct{})}, nil
	}
	return openSSH(ctx, normalized, options)
}

func openSSH(ctx context.Context, value config.Context, options Options) (*Session, error) {
	setDefaults(&options)
	tempDir, err := options.CreateTempDir()
	if err != nil {
		return nil, &contract.CLIError{Code: "ssh_start_failed", Message: "could not create private SSH control directory", Cause: err}
	}
	controlPath := filepath.Join(tempDir, "c")
	masterArgs := []string{
		"-M", "-S", controlPath, "-N", "-n", "-T",
		"-o", "ForkAfterAuthentication=no",
		"-o", "SessionType=none",
		"-o", "ControlMaster=yes",
		"-o", "ControlPersist=no",
		"-o", "ExitOnForwardFailure=yes",
		"-o", "ClearAllForwardings=yes",
		"-o", "ServerAliveInterval=15",
		"-o", "ServerAliveCountMax=3",
	}
	if options.NonInteractive {
		masterArgs = append(masterArgs, "-o", "BatchMode=yes")
	}
	if value.SSHPort != 0 {
		masterArgs = append(masterArgs, "-p", fmt.Sprintf("%d", value.SSHPort))
	}
	masterArgs = append(masterArgs, value.SSHTarget)
	process, err := options.Start(options.Executable, masterArgs, strings.NewReader(""), options.Stderr)
	if err != nil {
		cleanupErr := options.RemoveAll(tempDir)
		code, message := "ssh_start_failed", "SSH master could not be started"
		if errors.Is(err, exec.ErrNotFound) || isExecutableMissing(err) {
			code, message = "ssh_not_found", "ssh executable was not found"
		}
		value := &contract.CLIError{Code: code, Message: message, Cause: err}
		if cleanupErr != nil {
			value.Details = map[string]any{"cleanup_error": cleanupErr.Error()}
			value.Cause = errors.Join(err, cleanupErr)
		}
		return nil, value
	}
	session := &Session{
		supervisor: supervise(process), cleanup: options.CleanupTimeout, controlTimeout: options.ControlTimeout,
		executable: options.Executable, target: value.SSHTarget, sshPort: value.SSHPort,
		controlPath: controlPath, tempDir: tempDir, runControl: options.RunControl,
		removeAll: options.RemoveAll, closeComplete: make(chan struct{}),
	}
	startupCtx, cancelStartup := context.WithTimeout(ctx, options.StartupTimeout)
	defer cancelStartup()
	fail := func(code, message string, cause error) (*Session, error) {
		cleanupErr := session.Close()
		details := map[string]any{}
		if cleanupErr != nil {
			details["cleanup_error"] = cleanupErr.Error()
		}
		value := &contract.CLIError{Code: code, Message: message, Cause: cause}
		if len(details) > 0 {
			value.Details = details
		}
		return nil, value
	}
	early := func() (*Session, error) {
		return fail("ssh_early_exit", "SSH master exited before the tunnel became ready", nil)
	}

	checkArgs := controlArgs(controlPath, value.SSHPort, "check", "", value.SSHTarget)
	var checkErr error
	for {
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		controlCtx, cancel := boundedContext(startupCtx, options.ControlTimeout)
		checkErr = options.RunControl(controlCtx, options.Executable, checkArgs)
		cancel()
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		if checkErr == nil {
			session.controlReady = true
			break
		}
		select {
		case <-ctx.Done():
			return fail("interrupted", "SSH tunnel startup was interrupted", ctx.Err())
		case <-startupCtx.Done():
			return fail("ssh_control_unavailable", "SSH ControlMaster did not become available before the startup timeout", checkErr)
		case <-session.supervisor.done:
			return early()
		case <-time.After(20 * time.Millisecond):
		}
	}

	var baseURL string
	var forwardErr error
	for attempt := 0; attempt < options.ForwardAttempts; attempt++ {
		if code, message, cause, failed := startupState(ctx, startupCtx); failed {
			return fail(code, message, cause)
		}
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		port, allocateErr := options.AllocatePort()
		if code, message, cause, failed := startupState(ctx, startupCtx); failed {
			return fail(code, message, cause)
		}
		if allocateErr != nil {
			forwardErr = allocateErr
			continue
		}
		forward := fmt.Sprintf("127.0.0.1:%d:127.0.0.1:%d", port, value.RemoteAPIPort)
		forwardArgs := controlArgs(controlPath, value.SSHPort, "forward", forward, value.SSHTarget)
		controlCtx, cancel := boundedContext(startupCtx, options.ControlTimeout)
		forwardErr = options.RunControl(controlCtx, options.Executable, forwardArgs)
		cancel()
		if code, message, cause, failed := startupState(ctx, startupCtx); failed {
			return fail(code, message, cause)
		}
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		if forwardErr == nil {
			baseURL = fmt.Sprintf("http://127.0.0.1:%d", port)
			break
		}
	}
	if baseURL == "" {
		if code, message, cause, failed := startupState(ctx, startupCtx); failed {
			return fail(code, message, cause)
		}
		return fail("ssh_forward_failed", "SSH could not establish a local port forward", forwardErr)
	}

	var probeErr error
	for {
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		probeCtx, cancel := boundedContext(startupCtx, 250*time.Millisecond)
		probeErr = options.Probe(probeCtx, baseURL)
		cancel()
		if exited, _ := session.supervisor.exited(); exited {
			return early()
		}
		if probeErr == nil {
			session.BaseURL = baseURL
			return session, nil
		}
		select {
		case <-ctx.Done():
			return fail("interrupted", "SSH tunnel startup was interrupted", ctx.Err())
		case <-startupCtx.Done():
			return fail("ssh_health_unavailable", "forwarded H3 health did not become available before the startup timeout", probeErr)
		case <-session.supervisor.done:
			return early()
		case <-time.After(20 * time.Millisecond):
		}
	}
}

func controlArgs(controlPath string, sshPort int, operation, forward, target string) []string {
	args := []string{"-F", "none", "-S", controlPath, "-O", operation}
	if forward != "" {
		args = append(args,
			"-o", "ClearAllForwardings=no",
			"-o", "ExitOnForwardFailure=yes",
			"-L", forward,
		)
	}
	if sshPort != 0 {
		args = append(args, "-p", fmt.Sprintf("%d", sshPort))
	}
	return append(args, target)
}

func startupState(parent, startup context.Context) (string, string, error, bool) {
	if err := parent.Err(); err != nil {
		return "interrupted", "SSH tunnel startup was interrupted", err, true
	}
	if err := startup.Err(); err != nil {
		return "ssh_start_timeout", "SSH forwarding did not complete before the startup timeout", err, true
	}
	return "", "", nil, false
}

func boundedContext(parent context.Context, timeout time.Duration) (context.Context, context.CancelFunc) {
	if timeout <= 0 {
		return context.WithCancel(parent)
	}
	return context.WithTimeout(parent, timeout)
}

func (s *Session) Close() error {
	if s == nil {
		return nil
	}
	s.closeOnce.Do(func() {
		defer close(s.closeComplete)
		if s.supervisor == nil {
			return
		}
		cleanupCtx, cancel := context.WithTimeout(context.Background(), s.cleanup)
		defer cancel()
		var cleanupErrors []error
		exitAccepted := false
		exitedAfterAcceptance := false
		forcedStop := false
		if exited, _ := s.supervisor.exited(); !exited {
			if s.controlReady {
				exitArgs := controlArgs(s.controlPath, s.sshPort, "exit", "", s.target)
				controlCtx, controlCancel := boundedContext(cleanupCtx, s.controlTimeout)
				err := s.runControl(controlCtx, s.executable, exitArgs)
				controlCancel()
				if err != nil {
					cleanupErrors = append(cleanupErrors, fmt.Errorf("request SSH master exit: %w", err))
				} else {
					exitAccepted = true
					exitedAfterAcceptance = waitForGracefulExit(cleanupCtx, s.supervisor.done, s.cleanup)
				}
			}
			if exited, _ := s.supervisor.exited(); exited && exitAccepted {
				exitedAfterAcceptance = true
			} else if !exited {
				stopErr := s.supervisor.process.Stop(cleanupCtx)
				switch {
				case stopErr == nil:
					forcedStop = true
				case exitAccepted && isProcessDone(stopErr):
					exitedAfterAcceptance = true
				case !isProcessDone(stopErr):
					cleanupErrors = append(cleanupErrors, fmt.Errorf("stop SSH master: %w", stopErr))
				}
			}
		}
		select {
		case <-s.supervisor.done:
			_, waitErr := s.supervisor.exited()
			controlledNaturalExit := exitAccepted && !forcedStop && exitedAfterAcceptance
			if waitErr != nil && !isProcessDone(waitErr) && !controlledNaturalExit {
				cleanupErrors = append(cleanupErrors, fmt.Errorf("reap SSH master: %w", waitErr))
			}
		case <-cleanupCtx.Done():
			cleanupErrors = append(cleanupErrors, fmt.Errorf("SSH master cleanup timed out"))
		}
		if err := s.removeAll(s.tempDir); err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("remove SSH control directory: %w", err))
		}
		if len(cleanupErrors) > 0 {
			cause := errors.Join(cleanupErrors...)
			s.closeErr = &contract.CLIError{Code: "ssh_cleanup_failed", Message: "SSH tunnel cleanup failed", Details: map[string]any{"cleanup_error": cause.Error()}, Cause: cause}
		}
	})
	<-s.closeComplete
	return s.closeErr
}

func waitForGracefulExit(ctx context.Context, done <-chan struct{}, cleanup time.Duration) bool {
	wait := 500 * time.Millisecond
	if half := cleanup / 2; half > 0 && half < wait {
		wait = half
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-done:
		return true
	case <-timer.C:
	case <-ctx.Done():
	}
	return false
}

func setDefaults(options *Options) {
	if options.Executable == "" {
		options.Executable = "ssh"
	}
	if options.StartupTimeout <= 0 {
		options.StartupTimeout = 10 * time.Second
	}
	if options.CleanupTimeout <= 0 {
		options.CleanupTimeout = 2 * time.Second
	}
	if options.ControlTimeout <= 0 {
		options.ControlTimeout = 500 * time.Millisecond
	}
	if options.ForwardAttempts <= 0 {
		options.ForwardAttempts = 3
	}
	if options.Stderr == nil {
		options.Stderr = io.Discard
	}
	if options.Start == nil {
		options.Start = startCommand
	}
	if options.RunControl == nil {
		options.RunControl = runControl
	}
	if options.Probe == nil {
		options.Probe = probeH3Health
	}
	if options.AllocatePort == nil {
		options.AllocatePort = allocatePort
	}
	if options.CreateTempDir == nil {
		options.CreateTempDir = createTempDir
	}
	if options.RemoveAll == nil {
		options.RemoveAll = os.RemoveAll
	}
}

type commandProcess struct{ command *exec.Cmd }

func (p commandProcess) Wait() error  { return p.command.Wait() }
func (p commandProcess) Exited() bool { return processExited(p.command.Process) }
func (p commandProcess) Stop(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if p.command.Process == nil {
		return nil
	}
	return p.command.Process.Kill()
}

func startCommand(name string, args []string, stdin io.Reader, stderr io.Writer) (Process, error) {
	command := exec.Command(name, args...)
	command.Stdin, command.Stderr, command.Stdout = stdin, stderr, io.Discard
	if err := command.Start(); err != nil {
		return nil, err
	}
	return commandProcess{command: command}, nil
}

func runControl(ctx context.Context, name string, args []string) error {
	command := exec.CommandContext(ctx, name, args...)
	command.Stdin, command.Stdout = strings.NewReader(""), io.Discard
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		diagnostic := strings.TrimSpace(stderr.String())
		if len(diagnostic) > 2048 {
			diagnostic = diagnostic[:2048]
		}
		if diagnostic != "" {
			return fmt.Errorf("%w: %s", err, diagnostic)
		}
		return err
	}
	return nil
}

func probeH3Health(ctx context.Context, baseURL string) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/health", nil)
	if err != nil {
		return err
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	contentType, _, contentTypeErr := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if contentTypeErr != nil || !strings.EqualFold(contentType, "application/json") {
		return fmt.Errorf("H3 health response must be application/json")
	}
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusServiceUnavailable {
		return fmt.Errorf("unexpected H3 health status %d", response.StatusCode)
	}
	const maxHealthResponse = 64 << 10
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxHealthResponse+1))
	if err != nil {
		return fmt.Errorf("could not read H3 health response: %w", err)
	}
	if len(raw) > maxHealthResponse {
		return fmt.Errorf("H3 health response exceeds %d bytes", maxHealthResponse)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var body map[string]any
	if err := decoder.Decode(&body); err != nil {
		return fmt.Errorf("invalid H3 health response: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return fmt.Errorf("H3 health response contains trailing content")
	}
	status, _ := body["status"].(string)
	if response.StatusCode == http.StatusOK && status == "ok" && body["comfyui"] == "ok" {
		return nil
	}
	if response.StatusCode == http.StatusServiceUnavailable && status == "degraded" {
		errorBody, ok := body["error"].(map[string]any)
		code, codeOK := errorBody["code"].(string)
		message, messageOK := errorBody["message"].(string)
		if ok && codeOK && messageOK && code != "" && message != "" {
			return nil
		}
	}
	return fmt.Errorf("response is not an H3 health contract")
}

func allocatePort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

func createTempDir() (string, error) {
	directory, err := os.MkdirTemp("", "h3ctl-ssh-")
	if err != nil {
		return "", err
	}
	if err := os.Chmod(directory, 0o700); err != nil {
		_ = os.RemoveAll(directory)
		return "", err
	}
	return directory, nil
}

func isExecutableMissing(err error) bool {
	var execError *exec.Error
	return errors.As(err, &execError)
}

func isProcessDone(err error) bool {
	if err == nil {
		return false
	}
	text := strings.ToLower(err.Error())
	return errors.Is(err, os.ErrProcessDone) || strings.Contains(text, "signal: killed") || strings.Contains(text, "process already finished")
}
