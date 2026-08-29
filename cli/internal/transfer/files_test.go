package transfer

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCollectDirectoryStableAndFiltered(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "b.png"), []byte("b"), 0o600)
	_ = os.WriteFile(filepath.Join(dir, "a.png"), []byte("a"), 0o600)
	_ = os.WriteFile(filepath.Join(dir, "ignore.mp4"), []byte("v"), 0o600)
	files, err := Collect(dir, true, []string{"*.png"})
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 2 || filepath.Base(files[0]) != "a.png" || filepath.Base(files[1]) != "b.png" {
		t.Fatalf("unexpected files: %v", files)
	}
}

func TestCollectDirectoryRequiresRecursive(t *testing.T) {
	if _, err := Collect(t.TempDir(), false, nil); err == nil {
		t.Fatal("expected error")
	}
}
func TestCollectRejectsBadPattern(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "x"), []byte("x"), 0o600)
	if _, err := Collect(dir, true, []string{"["}); err == nil {
		t.Fatal("expected error")
	}
}

func TestCollectRejectsSymlinks(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target.png")
	_ = os.WriteFile(target, []byte("x"), 0o600)
	link := filepath.Join(dir, "link.png")
	if err := os.Symlink(target, link); err != nil {
		t.Skip(err)
	}
	if _, err := Collect(link, false, nil); err == nil {
		t.Fatal("root symlink accepted")
	}
	if _, err := Collect(dir, true, nil); err == nil {
		t.Fatal("nested symlink accepted")
	}
}
