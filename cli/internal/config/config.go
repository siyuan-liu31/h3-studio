package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"unicode"
)

type Context struct {
	Server        string `json:"server,omitempty"`
	SSHTarget     string `json:"ssh_target,omitempty"`
	SSHPort       int    `json:"ssh_port,omitempty"`
	RemoteAPIPort int    `json:"remote_api_port,omitempty"`
}

const DefaultRemoteAPIPort = 6020

type File struct {
	Current  string             `json:"current,omitempty"`
	Contexts map[string]Context `json:"contexts"`
}

type Store struct{ Path string }

func (s Store) Update(mutator func(*File) error) error {
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		return err
	}
	lock, err := acquireFileLock(s.Path + ".lock")
	if err != nil {
		return err
	}
	defer lock.Close()
	value, err := s.Load()
	if err != nil {
		return err
	}
	if err := mutator(&value); err != nil {
		return err
	}
	return s.Save(value)
}

func DefaultPath() (string, error) {
	if path := strings.TrimSpace(os.Getenv("H3CTL_CONFIG")); path != "" {
		return path, nil
	}
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "h3ctl", "config.json"), nil
}

func (s Store) Load() (File, error) {
	value := File{Contexts: map[string]Context{}}
	raw, err := os.ReadFile(s.Path)
	if errors.Is(err, os.ErrNotExist) {
		return value, nil
	}
	if err != nil {
		return value, err
	}
	if err := json.Unmarshal(raw, &value); err != nil {
		return value, fmt.Errorf("parse config: %w", err)
	}
	if value.Contexts == nil {
		value.Contexts = map[string]Context{}
	}
	return value, nil
}

func (s Store) Save(value File) error {
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.Path), ".config-*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(append(raw, '\n')); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, s.Path)
}

func NormalizeServer(raw string) (string, error) {
	value := strings.TrimSpace(raw)
	parsed, err := url.Parse(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return "", fmt.Errorf("server must be an absolute http:// or https:// origin")
	}
	if parsed.User != nil {
		return "", fmt.Errorf("server must not contain user information")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", fmt.Errorf("server must not contain a query or fragment")
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return "", fmt.Errorf("server base paths are not supported")
	}
	parsed.Path, parsed.RawPath = "", ""
	return strings.TrimRight(parsed.String(), "/"), nil
}

func Resolve(store Store, name, override string) (string, Context, error) {
	if override != "" {
		server, err := NormalizeServer(override)
		return name, Context{Server: server}, err
	}
	value, err := store.Load()
	if err != nil {
		return "", Context{}, err
	}
	if name == "" {
		name = value.Current
	}
	if name == "" {
		if env := os.Getenv("H3_STUDIO_URL"); env != "" {
			server, err := NormalizeServer(env)
			return "environment", Context{Server: server}, err
		}
		name = "local"
		return name, Context{Server: "http://127.0.0.1:6020"}, nil
	}
	ctx, ok := value.Contexts[name]
	if !ok {
		return "", Context{}, fmt.Errorf("context %q not found", name)
	}
	ctx, err = NormalizeContext(ctx)
	if err != nil {
		return "", Context{}, fmt.Errorf("context %q is invalid: %w", name, err)
	}
	return name, ctx, nil
}

func NormalizeContext(value Context) (Context, error) {
	direct, ssh := strings.TrimSpace(value.Server) != "", strings.TrimSpace(value.SSHTarget) != ""
	if direct == ssh {
		return Context{}, fmt.Errorf("exactly one of server or ssh_target is required")
	}
	if direct {
		if value.SSHPort != 0 || value.RemoteAPIPort != 0 {
			return Context{}, fmt.Errorf("SSH ports require ssh_target")
		}
		server, err := NormalizeServer(value.Server)
		if err != nil {
			return Context{}, err
		}
		return Context{Server: server}, nil
	}
	target := strings.TrimSpace(value.SSHTarget)
	if err := ValidateSSHTarget(target); err != nil {
		return Context{}, err
	}
	if value.SSHPort < 0 || value.SSHPort > 65535 {
		return Context{}, fmt.Errorf("ssh_port must be between 1 and 65535 when set")
	}
	port := value.RemoteAPIPort
	if port == 0 {
		port = DefaultRemoteAPIPort
	}
	if port < 1 || port > 65535 {
		return Context{}, fmt.Errorf("remote_api_port must be between 1 and 65535")
	}
	return Context{SSHTarget: target, SSHPort: value.SSHPort, RemoteAPIPort: port}, nil
}

func ValidateSSHTarget(target string) error {
	if target == "" || strings.HasPrefix(target, "-") {
		return fmt.Errorf("ssh_target must be non-empty and must not start with '-'")
	}
	for _, char := range target {
		if unicode.IsSpace(char) || unicode.IsControl(char) {
			return fmt.Errorf("ssh_target must not contain whitespace or control characters")
		}
	}
	return nil
}
