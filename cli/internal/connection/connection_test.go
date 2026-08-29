package connection

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"h3studio/cli/internal/config"
	"h3studio/cli/internal/contract"
)

type fakeProcess struct {
	done       chan struct{}
	once       sync.Once
	waitErr    error
	stopErr    error
	stopCloses bool
	stopCalls  atomic.Int32
}

type processDoneStopRace struct {
	done        chan struct{}
	once        sync.Once
	raceEnabled atomic.Bool
	exitedCalls atomic.Int32
	completed   atomic.Bool
	stopCalls   atomic.Int32
}

func newProcessDoneStopRace() *processDoneStopRace {
	return &processDoneStopRace{done: make(chan struct{})}
}

func (p *processDoneStopRace) Wait() error {
	<-p.done
	return errors.New("exit status 255")
}

func (p *processDoneStopRace) Exited() bool {
	if !p.raceEnabled.Load() {
		return false
	}
	if p.exitedCalls.Add(1) == 2 {
		p.completed.Store(true)
	}
	// Model a stale process-state observation even though the process completes
	// concurrently with this second check. Wait remains deliberately unreaped.
	return false
}

func (p *processDoneStopRace) Stop(context.Context) error {
	p.stopCalls.Add(1)
	if !p.completed.Load() {
		return errors.New("stop called before process completed")
	}
	p.once.Do(func() { close(p.done) })
	return os.ErrProcessDone
}

func newFakeProcess(exited bool) *fakeProcess {
	value := &fakeProcess{done: make(chan struct{}), stopCloses: true}
	if exited {
		value.once.Do(func() { close(value.done) })
	}
	return value
}

func (p *fakeProcess) Wait() error { <-p.done; return p.waitErr }
func (p *fakeProcess) Exited() bool {
	select {
	case <-p.done:
		return true
	default:
		return false
	}
}
func (p *fakeProcess) Stop(ctx context.Context) error {
	p.stopCalls.Add(1)
	if err := ctx.Err(); err != nil {
		return err
	}
	if p.stopCloses {
		p.once.Do(func() { close(p.done) })
	}
	return p.stopErr
}

func TestControlExitAcceptanceScopesNonzeroMasterWait(t *testing.T) {
	t.Run("accepted exit status 255 is controlled shutdown", func(t *testing.T) {
		process := newFakeProcess(false)
		process.waitErr = errors.New("exit status 255")
		options, _ := fakeOptions(t, process)
		options.RunControl = func(_ context.Context, _ string, args []string) error {
			if operationOf(args) == "exit" {
				go func() {
					time.Sleep(5 * time.Millisecond)
					process.once.Do(func() { close(process.done) })
				}()
			}
			return nil
		}
		session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
		if err != nil {
			t.Fatal(err)
		}
		if err := session.Close(); err != nil {
			t.Fatalf("accepted control exit was treated as cleanup failure: %v", err)
		}
		if process.stopCalls.Load() != 0 {
			t.Fatalf("master was stopped before its accepted control exit completed")
		}
	})

	t.Run("rejected exit status 255 remains cleanup failure", func(t *testing.T) {
		process := newFakeProcess(false)
		process.waitErr = errors.New("exit status 255")
		options, _ := fakeOptions(t, process)
		options.RunControl = func(_ context.Context, _ string, args []string) error {
			if operationOf(args) == "exit" {
				process.once.Do(func() { close(process.done) })
				return errors.New("control exit rejected")
			}
			return nil
		}
		session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
		if err != nil {
			t.Fatal(err)
		}
		err = session.Close()
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "ssh_cleanup_failed" {
			t.Fatalf("error=%#v", err)
		}
		details, ok := cliError.Details.(map[string]any)
		cleanupDetail, detailOK := details["cleanup_error"].(string)
		if !ok || !detailOK || !strings.Contains(cleanupDetail, "exit status 255") {
			t.Fatalf("details=%#v", cliError.Details)
		}
	})

	t.Run("status 255 after forced stop is not attributed to control exit", func(t *testing.T) {
		process := newFakeProcess(false)
		process.waitErr = errors.New("exit status 255")
		options, _ := fakeOptions(t, process)
		session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
		if err != nil {
			t.Fatal(err)
		}
		err = session.Close()
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "ssh_cleanup_failed" || process.stopCalls.Load() != 1 {
			t.Fatalf("error=%#v stopCalls=%d", err, process.stopCalls.Load())
		}
	})
}

func TestAcceptedExitProcessDoneStopRaceIsNaturalShutdown(t *testing.T) {
	process := newProcessDoneStopRace()
	options, _ := fakeOptions(t, nil)
	options.CleanupTimeout = 20 * time.Millisecond
	options.Start = func(string, []string, io.Reader, io.Writer) (Process, error) {
		return process, nil
	}
	session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
	if err != nil {
		t.Fatal(err)
	}
	process.raceEnabled.Store(true)
	if err := session.Close(); err != nil {
		t.Fatalf("ProcessDone race was treated as forced cleanup: %v", err)
	}
	if !process.completed.Load() || process.exitedCalls.Load() < 2 || process.stopCalls.Load() != 1 {
		t.Fatalf("race was not exercised: completed=%v exitedCalls=%d stopCalls=%d", process.completed.Load(), process.exitedCalls.Load(), process.stopCalls.Load())
	}
}

func operationOf(args []string) string {
	for index, value := range args {
		if value == "-O" && index+1 < len(args) {
			return args[index+1]
		}
	}
	return ""
}

func countArg(args []string, target string) int {
	count := 0
	for _, value := range args {
		if value == target {
			count++
		}
	}
	return count
}

func containsPair(args []string, first, second string) bool {
	for index := 0; index+1 < len(args); index++ {
		if args[index] == first && args[index+1] == second {
			return true
		}
	}
	return false
}

func fakeOptions(t *testing.T, process *fakeProcess) (Options, *[][]string) {
	t.Helper()
	commands := &[][]string{}
	return Options{
		StartupTimeout:  100 * time.Millisecond,
		CleanupTimeout:  50 * time.Millisecond,
		ControlTimeout:  20 * time.Millisecond,
		ForwardAttempts: 3,
		AllocatePort:    func() (int, error) { return 41234, nil },
		CreateTempDir: func() (string, error) {
			return os.MkdirTemp(t.TempDir(), "control-")
		},
		Start: func(_ string, args []string, stdin io.Reader, _ io.Writer) (Process, error) {
			buffer := make([]byte, 1)
			if count, err := stdin.Read(buffer); count != 0 || !errors.Is(err, io.EOF) {
				t.Fatalf("master stdin is not EOF: count=%d err=%v", count, err)
			}
			*commands = append(*commands, append([]string{}, args...))
			return process, nil
		},
		RunControl: func(_ context.Context, _ string, args []string) error {
			*commands = append(*commands, append([]string{}, args...))
			return nil
		},
		Probe: func(context.Context, string) error { return nil },
	}, commands
}

func TestControlMasterReadyArgvAndClose(t *testing.T) {
	process := newFakeProcess(false)
	options, commands := fakeOptions(t, process)
	options.NonInteractive = true
	var controlDir string
	originalTemp := options.CreateTempDir
	options.CreateTempDir = func() (string, error) {
		value, err := originalTemp()
		controlDir = value
		return value, err
	}
	session, err := Open(context.Background(), config.Context{SSHTarget: "dev-alias", SSHPort: 2222}, options)
	if err != nil {
		t.Fatal(err)
	}
	if session.BaseURL != "http://127.0.0.1:41234" {
		t.Fatalf("base URL=%q", session.BaseURL)
	}
	info, statErr := os.Stat(controlDir)
	if statErr != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("private control directory info=%v err=%v", info, statErr)
	}
	if len(*commands) != 3 {
		t.Fatalf("startup commands=%v", *commands)
	}
	master := strings.Join((*commands)[0], " ")
	for _, expected := range []string{"-M", "-S", "-N", "-n", "-T", "ForkAfterAuthentication=no", "SessionType=none", "ControlMaster=yes", "ControlPersist=no", "BatchMode=yes", "-p 2222", "dev-alias"} {
		if !strings.Contains(master, expected) {
			t.Errorf("master missing %q: %s", expected, master)
		}
	}
	if containsPair((*commands)[0], "-F", "none") {
		t.Fatalf("master must retain alias configuration: %v", (*commands)[0])
	}
	if !containsPair((*commands)[0], "-o", "ForkAfterAuthentication=no") || !containsPair((*commands)[0], "-o", "SessionType=none") || !containsPair((*commands)[0], "-o", "ClearAllForwardings=yes") {
		t.Fatalf("master does not override unsafe alias options: %v", (*commands)[0])
	}
	if operationOf((*commands)[1]) != "check" || operationOf((*commands)[2]) != "forward" {
		t.Fatalf("control sequence=%v", *commands)
	}
	for _, command := range (*commands)[1:] {
		if !containsPair(command, "-F", "none") || !containsPair(command, "-S", filepath.Join(controlDir, "c")) {
			t.Fatalf("control command is not isolated: %v", command)
		}
		if command[len(command)-1] != "dev-alias" {
			t.Fatalf("target is not a final argv token: %v", command)
		}
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	if operationOf((*commands)[3]) != "exit" {
		t.Fatalf("close did not use control exit: %v", *commands)
	}
	if !containsPair((*commands)[3], "-F", "none") || !containsPair((*commands)[3], "-S", filepath.Join(controlDir, "c")) {
		t.Fatalf("exit command is not isolated: %v", (*commands)[3])
	}
	forward := (*commands)[2]
	if countArg(forward, "-L") != 1 || !containsPair(forward, "-o", "ClearAllForwardings=no") || !containsPair(forward, "-o", "ExitOnForwardFailure=yes") {
		t.Fatalf("forward command does not request exactly one isolated forward: %v", forward)
	}
	if _, err := os.Stat(controlDir); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("control directory remains: %v", err)
	}
	select {
	case <-process.done:
	default:
		t.Fatal("master was not reaped")
	}
}

func TestOpenSSHMasterOptionsOverrideAliasExecutionAndForwards(t *testing.T) {
	executable, err := exec.LookPath("ssh")
	if err != nil {
		t.Skip("OpenSSH is not installed")
	}
	configPath := filepath.Join(t.TempDir(), "config")
	configBody := "Host dev-alias\n" +
		"  HostName 127.0.0.1\n" +
		"  ForkAfterAuthentication yes\n" +
		"  SessionType default\n" +
		"  RemoteCommand echo-danger\n" +
		"  LocalForward 9012 127.0.0.1:6020\n"
	if err := os.WriteFile(configPath, []byte(configBody), 0o600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable,
		"-G", "-F", configPath, "-N", "-T",
		"-o", "ForkAfterAuthentication=no",
		"-o", "SessionType=none",
		"-o", "ClearAllForwardings=yes",
		"dev-alias",
	)
	output, err := command.Output()
	if err != nil {
		t.Fatalf("ssh -G failed: %v", err)
	}
	effective := string(output)
	for _, expected := range []string{"forkafterauthentication no\n", "sessiontype none\n", "clearallforwardings yes\n"} {
		if !strings.Contains(effective, expected) {
			t.Fatalf("ssh -G missing %q:\n%s", expected, effective)
		}
	}
	if strings.Contains(effective, "localforward ") {
		t.Fatalf("alias LocalForward survived ClearAllForwardings=yes:\n%s", effective)
	}
}

func TestOccupiedH3PortWithForwardFailureRetriesDifferentPort(t *testing.T) {
	occupied := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "comfyui": "ok"})
	}))
	defer occupied.Close()
	parts := strings.Split(occupied.URL, ":")
	occupiedPort, _ := strconv.Atoi(parts[len(parts)-1])
	process := newFakeProcess(false)
	options, _ := fakeOptions(t, process)
	ports := []int{occupiedPort, 41236}
	options.AllocatePort = func() (int, error) { value := ports[0]; ports = ports[1:]; return value, nil }
	var forwards, probes int
	options.RunControl = func(_ context.Context, _ string, args []string) error {
		if operationOf(args) == "forward" {
			forwards++
			if forwards == 1 {
				return errors.New("port already allocated")
			}
		}
		return nil
	}
	options.Probe = func(_ context.Context, baseURL string) error {
		probes++
		if baseURL != "http://127.0.0.1:41236" {
			t.Fatalf("probe used unowned listener: %s", baseURL)
		}
		return nil
	}
	session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
	if err != nil {
		t.Fatal(err)
	}
	if forwards != 2 || probes != 1 {
		t.Fatalf("forwards=%d probes=%d", forwards, probes)
	}
	_ = session.Close()
}

func TestControlAndStartupFailures(t *testing.T) {
	tests := []struct {
		name string
		set  func(*Options, *fakeProcess)
		code string
	}{
		{name: "master early exit", code: "ssh_early_exit", set: func(_ *Options, process *fakeProcess) { process.once.Do(func() { close(process.done) }) }},
		{name: "check timeout", code: "ssh_control_unavailable", set: func(options *Options, _ *fakeProcess) {
			options.RunControl = func(context.Context, string, []string) error { return errors.New("no control") }
		}},
		{name: "forward exhausted", code: "ssh_forward_failed", set: func(options *Options, _ *fakeProcess) {
			options.RunControl = func(_ context.Context, _ string, args []string) error {
				if operationOf(args) == "forward" {
					return errors.New("bind failed")
				}
				return nil
			}
		}},
		{name: "health failed", code: "ssh_health_unavailable", set: func(options *Options, _ *fakeProcess) {
			options.Probe = func(context.Context, string) error { return errors.New("wrong service") }
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			process := newFakeProcess(false)
			options, _ := fakeOptions(t, process)
			options.StartupTimeout = 30 * time.Millisecond
			test.set(&options, process)
			_, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
			var cliError *contract.CLIError
			if !errors.As(err, &cliError) || cliError.Code != test.code {
				t.Fatalf("error=%#v", err)
			}
		})
	}
}

func TestMissingBinaryAndMaliciousTarget(t *testing.T) {
	options := Options{Start: func(string, []string, io.Reader, io.Writer) (Process, error) { return nil, exec.ErrNotFound }}
	_, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
	var cliError *contract.CLIError
	if !errors.As(err, &cliError) || cliError.Code != "ssh_not_found" {
		t.Fatalf("error=%#v", err)
	}
	called := false
	options.Start = func(string, []string, io.Reader, io.Writer) (Process, error) { called = true; return nil, nil }
	for _, target := range []string{"", "-oProxyCommand=bad", "dev host", "dev\nother"} {
		if _, err := Open(context.Background(), config.Context{SSHTarget: target}, options); err == nil {
			t.Fatalf("accepted %q", target)
		}
	}
	if called {
		t.Fatal("starter called for invalid target")
	}
}

func TestMasterStartFailurePreservesPrimaryCodeAndCleanupDetails(t *testing.T) {
	tests := []struct {
		name     string
		startErr error
		wantCode string
	}{
		{name: "missing", startErr: exec.ErrNotFound, wantCode: "ssh_not_found"},
		{name: "start", startErr: errors.New("permission denied"), wantCode: "ssh_start_failed"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			options := Options{
				CreateTempDir: func() (string, error) { return filepath.Join(t.TempDir(), "control"), nil },
				Start:         func(string, []string, io.Reader, io.Writer) (Process, error) { return nil, test.startErr },
				RemoveAll:     func(string) error { return errors.New("cleanup denied") },
			}
			_, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
			var cliError *contract.CLIError
			if !errors.As(err, &cliError) || cliError.Code != test.wantCode {
				t.Fatalf("error=%#v", err)
			}
			details, ok := cliError.Details.(map[string]any)
			if !ok || details["cleanup_error"] != "cleanup denied" {
				t.Fatalf("cleanup details=%#v", cliError.Details)
			}
		})
	}
}

func TestForwardCancellationAndStartupDeadlineClassification(t *testing.T) {
	t.Run("cancel after allocate", func(t *testing.T) {
		process := newFakeProcess(false)
		options, _ := fakeOptions(t, process)
		ctx, cancel := context.WithCancel(context.Background())
		var forwards int
		options.AllocatePort = func() (int, error) {
			cancel()
			return 41234, nil
		}
		options.RunControl = func(_ context.Context, _ string, args []string) error {
			if operationOf(args) == "forward" {
				forwards++
			}
			return nil
		}
		_, err := Open(ctx, config.Context{SSHTarget: "dev"}, options)
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "interrupted" || forwards != 0 {
			t.Fatalf("error=%#v forwards=%d", err, forwards)
		}
	})

	t.Run("deadline after forward", func(t *testing.T) {
		process := newFakeProcess(false)
		options, _ := fakeOptions(t, process)
		options.StartupTimeout = 20 * time.Millisecond
		options.ControlTimeout = 100 * time.Millisecond
		options.RunControl = func(ctx context.Context, _ string, args []string) error {
			if operationOf(args) == "forward" {
				<-ctx.Done()
				return ctx.Err()
			}
			return nil
		}
		_, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "ssh_start_timeout" {
			t.Fatalf("error=%#v", err)
		}
	})

	t.Run("cancel after forward", func(t *testing.T) {
		process := newFakeProcess(false)
		options, _ := fakeOptions(t, process)
		ctx, cancel := context.WithCancel(context.Background())
		options.RunControl = func(_ context.Context, _ string, args []string) error {
			if operationOf(args) == "forward" {
				cancel()
				return errors.New("control interrupted")
			}
			return nil
		}
		_, err := Open(ctx, config.Context{SSHTarget: "dev"}, options)
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "interrupted" {
			t.Fatalf("error=%#v", err)
		}
	})
}

func TestCloseFailureIsBoundedAndConcurrentCallersShareResult(t *testing.T) {
	process := newFakeProcess(false)
	process.stopCloses = false
	process.stopErr = errors.New("stop denied")
	options, _ := fakeOptions(t, process)
	options.CleanupTimeout = 15 * time.Millisecond
	options.RunControl = func(_ context.Context, _ string, args []string) error {
		if operationOf(args) == "exit" {
			return errors.New("exit failed")
		}
		return nil
	}
	session, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	results := make(chan error, 8)
	var group sync.WaitGroup
	for range 8 {
		group.Add(1)
		go func() { defer group.Done(); results <- session.Close() }()
	}
	group.Wait()
	close(results)
	var message string
	for err := range results {
		var cliError *contract.CLIError
		if !errors.As(err, &cliError) || cliError.Code != "ssh_cleanup_failed" {
			t.Fatalf("error=%#v", err)
		}
		if message == "" {
			message = err.Error()
		} else if err.Error() != message {
			t.Fatalf("inconsistent close error: %v", err)
		}
	}
	if time.Since(started) > 100*time.Millisecond {
		t.Fatalf("close was unbounded: %v", time.Since(started))
	}
}

func TestStrictHealthProbe(t *testing.T) {
	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
		ok          bool
	}{
		{name: "ok", status: 200, contentType: "application/json", body: `{"status":"ok","comfyui":"ok"}`, ok: true},
		{name: "degraded", status: 503, contentType: "application/json; charset=utf-8", body: `{"status":"degraded","error":{"code":"offline","message":"offline"}}`, ok: true},
		{name: "wrong content", status: 200, contentType: "text/plain", body: `{"status":"ok","comfyui":"ok"}`},
		{name: "jsonp content", status: 200, contentType: "application/jsonp", body: `{"status":"ok","comfyui":"ok"}`},
		{name: "malformed content", status: 200, contentType: "application/json; charset", body: `{"status":"ok","comfyui":"ok"}`},
		{name: "trailing garbage", status: 200, contentType: "application/json", body: `{"status":"ok","comfyui":"ok"} garbage`},
		{name: "second json", status: 200, contentType: "application/json", body: `{"status":"ok","comfyui":"ok"} {}`},
		{name: "oversize tail", status: 200, contentType: "application/json", body: `{"status":"ok","comfyui":"ok"}` + strings.Repeat(" ", 64<<10)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", test.contentType)
				w.WriteHeader(test.status)
				_, _ = io.WriteString(w, test.body)
			}))
			defer server.Close()
			err := probeH3Health(context.Background(), server.URL)
			if (err == nil) != test.ok {
				t.Fatalf("ok=%v err=%v", test.ok, err)
			}
		})
	}
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok","comfyui":"ok"}`)
	}))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, http.StatusFound) }))
	defer redirect.Close()
	if err := probeH3Health(context.Background(), redirect.URL); err == nil {
		t.Fatal("redirect was accepted")
	}
}

func TestControlCommandTimeoutIsBounded(t *testing.T) {
	process := newFakeProcess(false)
	options, _ := fakeOptions(t, process)
	options.StartupTimeout = 25 * time.Millisecond
	options.RunControl = func(ctx context.Context, _ string, _ []string) error { <-ctx.Done(); return ctx.Err() }
	started := time.Now()
	_, err := Open(context.Background(), config.Context{SSHTarget: "dev"}, options)
	if err == nil || time.Since(started) > 150*time.Millisecond {
		t.Fatalf("err=%v elapsed=%v", err, time.Since(started))
	}
}

func TestDirectDoesNotStartSSH(t *testing.T) {
	var called atomic.Bool
	session, err := Open(context.Background(), config.Context{Server: "https://example.test"}, Options{Start: func(string, []string, io.Reader, io.Writer) (Process, error) { called.Store(true); return nil, nil }})
	if err != nil || called.Load() || session.BaseURL != "https://example.test" || session.Close() != nil {
		t.Fatalf("session=%#v called=%v err=%v", session, called.Load(), err)
	}
}

func TestControlPathIsShort(t *testing.T) {
	directory, err := createTempDir()
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	if len(filepath.Join(directory, "c")) > 100 {
		t.Fatalf("control path unexpectedly long: %s", directory)
	}
}
