package resource

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseLocators(t *testing.T) {
	temp := filepath.Join(t.TempDir(), "frame.png")
	if err := os.WriteFile(temp, []byte("image"), 0o600); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		raw   string
		kind  Kind
		id    string
		index int
	}{
		{temp, Local, "", 0}, {"file://" + temp, Local, "", 0},
		{"file://localhost" + temp, Local, "", 0},
		{"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Asset, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 0},
		{"job:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb#2", Job, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 2},
		{"media:cccccccccccccccccccccccccccccccc", Media, "cccccccccccccccccccccccccccccccc", 0},
		{"h3://gpu-a/assets/dddddddddddddddddddddddddddddddd", Remote, "dddddddddddddddddddddddddddddddd", 0},
	}
	for _, test := range tests {
		t.Run(test.raw, func(t *testing.T) {
			got, err := Parse(test.raw)
			if err != nil {
				t.Fatal(err)
			}
			if got.Kind != test.kind || got.ID != test.id || got.Index != test.index {
				t.Fatalf("got %#v", got)
			}
		})
	}
}

func TestParseLocatorRejectsInvalidInputs(t *testing.T) {
	for _, raw := range []string{"", "asset:bad/id", "asset:ABCDEF0123456789ABCDEF0123456789", "job:id#-1", "job:id#x", "media:", "h3://gpu/assets", "h3://user:secret@gpu/assets/a", "h3://gpu/assets/a?q=secret", "h3://gpu/assets/a#secret", "file://remote.example/tmp/secret", "file://user:secret@localhost/tmp/a", "file:///tmp/a?q=secret", "https://user:secret@example/a", filepath.Join(t.TempDir(), "missing")} {
		if _, err := Parse(raw); err == nil {
			t.Errorf("expected %q to fail", raw)
		} else if strings.Contains(err.Error(), "secret") {
			t.Errorf("error leaked URI secret for %q: %v", raw, err)
		}
	}
	dir := t.TempDir()
	if _, err := Parse(dir); err == nil {
		t.Error("directory should fail")
	}
}

func TestDeriveSource(t *testing.T) {
	got := (Locator{Kind: Job, ID: "j", Index: 3}).DeriveSource()
	if got["type"] != "job" || got["index"] != 3 {
		t.Fatalf("unexpected source: %#v", got)
	}
}
