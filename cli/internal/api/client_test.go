package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"h3studio/cli/internal/contract"
)

func TestJSONAndAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/fail" {
			w.WriteHeader(429)
			_, _ = io.WriteString(w, `{"error":{"code":"busy","message":"retry"}}`)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
	defer server.Close()
	client := New(server.URL, time.Second)
	if _, err := client.Get(context.Background(), "/ok"); err != nil {
		t.Fatal(err)
	}
	_, err := client.Get(context.Background(), "/fail")
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "busy" || !typed.Retryable {
		t.Fatalf("unexpected error: %#v", err)
	}
}

func TestClientDoesNotInjectLegacyCredentialHeader(t *testing.T) {
	t.Setenv("H3_STUDIO_API_KEY", "must-not-be-sent")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if value := r.Header.Get("X-API-Key"); value != "" {
			t.Fatalf("unexpected credential header %q", value)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
	defer server.Close()
	if _, err := New(server.URL, time.Second).Get(context.Background(), "/health"); err != nil {
		t.Fatal(err)
	}
}

func TestStreamingMultipartUpload(t *testing.T) {
	content := []byte("video-content")
	path := filepath.Join(t.TempDir(), "clip.mp4")
	_ = os.WriteFile(path, content, 0o600)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if len(r.TransferEncoding) > 0 {
			t.Errorf("unexpected chunked transfer: %v", r.TransferEncoding)
		}
		if err := r.ParseMultipartForm(1024); err != nil {
			t.Fatal(err)
		}
		file, _, err := r.FormFile("file")
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		raw, _ := io.ReadAll(file)
		if string(raw) != string(content) || r.FormValue("kind") != "video" {
			t.Errorf("unexpected multipart")
		}
		w.WriteHeader(201)
		_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
	}))
	defer server.Close()
	value, err := New(server.URL, time.Second).Upload(context.Background(), path, "video")
	if err != nil {
		t.Fatal(err)
	}
	if value["asset_id"] != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" {
		t.Fatalf("unexpected: %v", value)
	}
}

func TestDownloadAtomicAndNoOverwrite(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "4")
		_, _ = io.WriteString(w, "data")
	}))
	defer server.Close()
	client := New(server.URL, time.Second)
	path := filepath.Join(t.TempDir(), "out.bin")
	value, err := client.Download(context.Background(), "/file", path, false)
	if err != nil {
		t.Fatal(err)
	}
	if value["bytes"] != int64(4) {
		t.Fatalf("unexpected value: %v", value)
	}
	if _, err := os.Stat(path + ".part"); !os.IsNotExist(err) {
		t.Fatal("part file remained")
	}
	if _, err := client.Download(context.Background(), "/file", path, false); err == nil {
		t.Fatal("expected overwrite rejection")
	}
	if _, err := client.Download(context.Background(), "/file", path, true); err != nil {
		t.Fatal(err)
	}
}

func TestRedirectIsNotFollowed(t *testing.T) {
	var received atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received.Add(1)
	}))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer source.Close()
	_, err := New(source.URL, time.Second).Get(context.Background(), "/redirect")
	if err == nil {
		t.Fatal("redirect unexpectedly succeeded")
	}
	if received.Load() != 0 {
		t.Fatal("redirect target was contacted")
	}
}

func TestTransferTimeoutIsSeparateFromControlTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(30 * time.Millisecond)
		if r.URL.Path == "/api/assets" {
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
			return
		}
		if r.URL.Path == "/media" {
			_, _ = io.WriteString(w, `{"receipt_id":"cccccccccccccccccccccccccccccccc"}`)
			return
		}
		if r.URL.Path == "/file" {
			_, _ = io.WriteString(w, "download")
			return
		}
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer server.Close()
	client := NewWithTimeouts(server.URL, 5*time.Millisecond, 100*time.Millisecond, 100*time.Millisecond)
	if _, err := client.Get(context.Background(), "/slow"); err == nil {
		t.Fatal("control request should time out")
	}
	path := filepath.Join(t.TempDir(), "x.bin")
	_ = os.WriteFile(path, []byte("x"), 0o600)
	if _, err := client.Upload(context.Background(), path, "auto"); err != nil {
		t.Fatalf("transfer used control timeout: %v", err)
	}
	if _, err := client.Download(context.Background(), "/file", filepath.Join(t.TempDir(), "out.bin"), false); err != nil {
		t.Fatalf("download used control timeout: %v", err)
	}
	var media map[string]any
	if err := client.JSONMedia(context.Background(), http.MethodPost, "/media", map[string]any{"operation": "frame"}, &media); err != nil {
		t.Fatalf("media request used control timeout: %v", err)
	}
}

func TestConcurrentNonForceDownloadsHaveOneAtomicWinner(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "content") }))
	defer server.Close()
	client := New(server.URL, time.Second)
	destination := filepath.Join(t.TempDir(), "same.bin")
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := client.Download(context.Background(), "/file", destination, false)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	success, failed := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else {
			failed++
		}
	}
	if success != 1 || failed != 1 {
		t.Fatalf("success=%d failed=%d", success, failed)
	}
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(destination), ".*.part"))
	if len(matches) != 0 {
		t.Fatalf("temp files remain: %v", matches)
	}
}

func TestRedirectPolicyCannotBeOverriddenByInjectedHTTPClient(t *testing.T) {
	var received atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { received.Add(1) }))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer source.Close()
	client := New(source.URL, time.Second)
	client.HTTP = source.Client() // source.Client normally follows redirects.
	if _, err := client.Get(context.Background(), "/redirect"); err == nil {
		t.Fatal("redirect unexpectedly succeeded")
	}
	if received.Load() != 0 {
		t.Fatal("injected HTTP client bypassed redirect protection")
	}
}

func TestForceFailurePreservesExistingDestination(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100")
		_, _ = io.WriteString(w, "short")
	}))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "out.bin")
	_ = os.WriteFile(path, []byte("old"), 0o600)
	if _, err := New(server.URL, time.Second).Download(context.Background(), "/file", path, true); err == nil {
		t.Fatal("expected truncated download")
	}
	raw, _ := os.ReadFile(path)
	if string(raw) != "old" {
		t.Fatalf("old destination lost: %q", raw)
	}
}
