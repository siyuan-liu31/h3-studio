package config

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpdateHelperProcess(t *testing.T) {
	name := os.Getenv("H3_CONFIG_TEST_NAME")
	if name == "" {
		return
	}
	store := Store{Path: os.Getenv("H3_CONFIG_TEST_PATH")}
	if err := store.Update(func(value *File) error {
		value.Contexts[name] = Context{Server: "https://" + name + ".example"}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestSaveRoundTripAndPermissions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "config.json")
	store := Store{Path: path}
	input := File{Current: "dev", Contexts: map[string]Context{"dev": {Server: "https://example.test"}}}
	if err := store.Save(input); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "secret") {
		t.Fatal("secret unexpectedly persisted")
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("permissions=%o", info.Mode().Perm())
	}
	loaded, err := store.Load()
	if err != nil || loaded.Current != "dev" || loaded.Contexts["dev"].Server != "https://example.test" {
		t.Fatalf("loaded=%v err=%v", loaded, err)
	}
}

func TestResolveDefaultsAndRejectsBadServer(t *testing.T) {
	store := Store{Path: filepath.Join(t.TempDir(), "missing.json")}
	t.Setenv("H3_STUDIO_URL", "")
	name, value, err := Resolve(store, "", "")
	if err != nil || name != "local" || value.Server != "http://127.0.0.1:6020" {
		t.Fatalf("%s %#v %v", name, value, err)
	}
	if _, err := NormalizeServer("ssh://host"); err == nil {
		t.Fatal("expected invalid server")
	}
	for _, value := range []string{"https://user:pass@example.test", "https://example.test/base", "https://example.test?q=1", "https://example.test/#fragment", "//example.test"} {
		if _, err := NormalizeServer(value); err == nil {
			t.Errorf("accepted unsafe server %q", value)
		}
	}
}

func TestLegacyAPIKeyFieldIsIgnoredAndRemovedOnSave(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(`{"current":"dev","contexts":{"dev":{"server":"https://example.test","api_key_env":"DO_NOT_PRINT"}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	store := Store{Path: path}
	value, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Save(value); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), "api_key") || strings.Contains(string(raw), "DO_NOT_PRINT") {
		t.Fatalf("legacy credential metadata survived save: %s", raw)
	}
}

func TestNormalizeSSHContext(t *testing.T) {
	value, err := NormalizeContext(Context{SSHTarget: "h3-dev"})
	if err != nil || value.RemoteAPIPort != 6020 {
		t.Fatalf("value=%#v err=%v", value, err)
	}
	for _, bad := range []Context{{}, {Server: "http://localhost:6020", SSHTarget: "dev"}, {SSHTarget: "-oProxyCommand=x"}, {SSHTarget: "dev host"}, {SSHTarget: "dev", SSHPort: 65536}, {SSHTarget: "dev", RemoteAPIPort: -1}} {
		if _, err := NormalizeContext(bad); err == nil {
			t.Fatalf("accepted %#v", bad)
		}
	}
}

func TestConcurrentCrossProcessUpdatesDoNotLoseContexts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	commands := make([]*exec.Cmd, 8)
	outputs := make([]bytes.Buffer, len(commands))
	for index := range commands {
		name := "agent" + string(rune('a'+index))
		command := exec.Command(os.Args[0], "-test.run=^TestUpdateHelperProcess$")
		command.Env = append(os.Environ(), "H3_CONFIG_TEST_PATH="+path, "H3_CONFIG_TEST_NAME="+name)
		command.Stdout = &outputs[index]
		command.Stderr = &outputs[index]
		commands[index] = command
		if err := command.Start(); err != nil {
			t.Fatal(err)
		}
	}
	for index, command := range commands {
		if err := command.Wait(); err != nil {
			t.Fatalf("helper failed: %v: %s", err, outputs[index].String())
		}
	}
	value, err := (Store{Path: path}).Load()
	if err != nil {
		t.Fatal(err)
	}
	if len(value.Contexts) != len(commands) {
		t.Fatalf("lost concurrent updates: %#v", value.Contexts)
	}
}

func TestResolveRevalidatesStoredServer(t *testing.T) {
	store := Store{Path: filepath.Join(t.TempDir(), "config.json")}
	if err := store.Save(File{Current: "bad", Contexts: map[string]Context{"bad": {Server: "https://user:secret@example.test"}}}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Resolve(store, "bad", ""); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("unsafe stored context error=%v", err)
	}
}
