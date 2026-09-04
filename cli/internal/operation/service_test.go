package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/contract"
)

const (
	serviceAssetID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	serviceJobID   = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	serviceMediaID = "cccccccccccccccccccccccccccccccc"
	serviceReqID   = "dddddddddddddddddddddddddddddddd"
)

func TestComposeVideoPinsProfilesRunsMergesAndAtomicallyDownloads(t *testing.T) {
	destination := filepath.Join(t.TempDir(), "final.mp4")
	var received map[string]any
	var mergeStarted atomic.Bool
	events := []map[string]any{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/capabilities":
			_ = json.NewEncoder(w).Encode(map[string]any{"profiles": []any{map[string]any{
				"id": "minimax-h3-fl2va", "version": "3", "manifest_sha256": strings.Repeat("e", 64), "available": true,
			}}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/video-projects":
			_ = json.NewDecoder(r.Body).Decode(&received)
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID})
		case r.Method == http.MethodPost && r.URL.Path == "/api/video-projects/"+serviceReqID+"/run":
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID, "status": "running"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/video-projects/"+serviceReqID+"/merge":
			mergeStarted.Store(true)
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID, "status": "merging"})
		case r.Method == http.MethodGet && r.URL.Path == "/api/video-projects/"+serviceReqID:
			status := "completed"
			if mergeStarted.Load() {
				status = "merged"
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID, "status": status})
		case r.Method == http.MethodGet && r.URL.Path == "/api/video-projects/"+serviceReqID+"/merged/download":
			_, _ = io.WriteString(w, "final-video")
		default:
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	spec := map[string]any{"title": "trilogy", "segments": []any{map[string]any{
		"continuation": "none",
		"request":      map[string]any{"profile_id": "minimax-h3-fl2va", "prompt": "shot one"},
	}}}
	result, err := serviceFor(server).ComposeVideo(context.Background(), spec, destination, false, WaitOptions{
		Timeout: time.Second, PollInterval: time.Millisecond,
		OnEvent: func(event map[string]any) { events = append(events, event) },
	})
	if err != nil {
		t.Fatal(err)
	}
	request := received["segments"].([]any)[0].(map[string]any)["request"].(map[string]any)
	if request["profile_version"] != "3" || request["profile_digest"] != strings.Repeat("e", 64) {
		t.Fatalf("profile was not pinned: %#v", request)
	}
	if spec["segments"].([]any)[0].(map[string]any)["request"].(map[string]any)["profile_version"] != nil {
		t.Fatal("input spec was mutated")
	}
	content, readErr := os.ReadFile(destination)
	if readErr != nil || string(content) != "final-video" {
		t.Fatalf("download=%q err=%v", content, readErr)
	}
	if result["project_id"] != serviceReqID || len(events) < 3 || events[0]["type"] != "project_created" {
		t.Fatalf("result=%#v events=%#v", result, events)
	}
}

func TestComposeVideoFailsBeforeMutationForUnavailableProfile(t *testing.T) {
	var mutatingCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/capabilities" {
			mutatingCalls.Add(1)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"profiles": []any{map[string]any{
			"id": "minimax-h3-fl2va", "available": false,
		}}})
	}))
	defer server.Close()
	_, err := serviceFor(server).ComposeVideo(context.Background(), map[string]any{
		"segments": []any{map[string]any{"request": map[string]any{"profile_id": "minimax-h3-fl2va"}}},
	}, filepath.Join(t.TempDir(), "never.mp4"), false, WaitOptions{})
	var typed *contract.CLIError
	if !errors.As(err, &typed) || typed.Code != "video_compose_failed" {
		t.Fatalf("unexpected error: %#v", err)
	}
	details, _ := typed.Details.(map[string]any)
	if details["phase"] != "profile" {
		t.Fatalf("unexpected error: %#v", err)
	}
	if mutatingCalls.Load() != 0 {
		t.Fatalf("profile rejection performed %d mutating requests", mutatingCalls.Load())
	}
}

func serviceFor(server *httptest.Server) *Service {
	return &Service{API: api.New(server.URL, time.Second), Context: "test", PollInterval: time.Millisecond}
}

func migrationInput(source, character string) map[string]any {
	return map[string]any{
		"version": "h3.character-migration/v1",
		"source":  source,
		"targets": []any{map[string]any{
			"character":      character,
			"source_subject": "the centered dancer",
		}},
	}
}

func TestPlanCharacterMigrationUsesCommonLocalAndCurrentRemoteLocators(t *testing.T) {
	localSource := filepath.Join(t.TempDir(), "source.mp4")
	localCharacter := filepath.Join(t.TempDir(), "character.png")
	for _, path := range []string{localSource, localCharacter} {
		if err := os.WriteFile(path, []byte("test media"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	for _, test := range []struct {
		name, source, character string
		wantUploads             int32
	}{
		{"local", localSource, localCharacter, 2},
		{"current_remote", "h3://test/assets/" + serviceAssetID, "h3://test/assets/" + serviceJobID, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			var uploads atomic.Int32
			var planned map[string]any
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/assets":
					n := uploads.Add(1)
					id := serviceAssetID
					if n == 2 {
						id = serviceJobID
					}
					w.WriteHeader(http.StatusCreated)
					_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": id})
				case "/api/video/character-migration/plan":
					_ = json.NewDecoder(r.Body).Decode(&planned)
					_ = json.NewEncoder(w).Encode(map[string]any{"project": map[string]any{"title": "migration"}})
				default:
					t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
				}
			}))
			defer server.Close()

			result, err := serviceFor(server).PlanCharacterMigration(context.Background(), migrationInput(test.source, test.character))
			if err != nil {
				t.Fatal(err)
			}
			if uploads.Load() != test.wantUploads || planned["source_asset_id"] != serviceAssetID {
				t.Fatalf("uploads=%d plan=%v", uploads.Load(), planned)
			}
			target := planned["targets"].([]any)[0].(map[string]any)
			if target["character_asset_id"] != serviceJobID || len(result["resolved_resources"].([]any)) != 2 {
				t.Fatalf("target=%v result=%v", target, result)
			}
		})
	}
}

func TestProduceCharacterMigrationRejectsExistingOutputBeforeRemoteMutation(t *testing.T) {
	destination := filepath.Join(t.TempDir(), "existing.mp4")
	if err := os.WriteFile(destination, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
	}))
	defer server.Close()
	input := migrationInput("asset:"+serviceAssetID, "asset:"+serviceJobID)
	input["to"] = destination
	_, err := serviceFor(server).ProduceCharacterMigration(context.Background(), input, WaitOptions{})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "output_exists" || requests.Load() != 0 {
		t.Fatalf("requests=%d err=%#v", requests.Load(), err)
	}
}

func TestProduceCharacterMigrationCtrlCKeepsServerProjectResumable(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var cancelRequests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/video/character-migration/plan":
			_ = json.NewEncoder(w).Encode(map[string]any{"project": map[string]any{"title": "migration", "segments": []any{}}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/video-projects":
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID})
		case r.Method == http.MethodPost && r.URL.Path == "/api/video-projects/"+serviceReqID+"/run":
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID, "status": "running"})
			go func() {
				time.Sleep(5 * time.Millisecond)
				cancel()
			}()
		case r.Method == http.MethodGet && r.URL.Path == "/api/video-projects/"+serviceReqID:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": serviceReqID, "status": "running"})
		default:
			if r.Method == http.MethodPost || r.Method == http.MethodDelete {
				cancelRequests.Add(1)
			}
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	input := migrationInput("asset:"+serviceAssetID, "asset:"+serviceJobID)
	input["to"] = filepath.Join(t.TempDir(), "migration.mp4")
	_, err := serviceFor(server).ProduceCharacterMigration(ctx, input, WaitOptions{Timeout: time.Second, PollInterval: time.Millisecond})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "character_migration_failed" {
		t.Fatalf("err=%#v", err)
	}
	details, _ := typed.Details.(map[string]any)
	if details["phase"] != "generate" || details["project_id"] != serviceReqID || cancelRequests.Load() != 0 {
		t.Fatalf("details=%v cancel_requests=%d", details, cancelRequests.Load())
	}
}

func TestWaitCompletedAfterTransientError(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n == 1 {
			w.WriteHeader(503)
			_, _ = w.Write([]byte(`{"error":{"code":"busy","message":"busy"}}`))
			return
		}
		status := "running"
		if n >= 3 {
			status = "completed"
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": status, "job_id": serviceJobID})
	}))
	defer server.Close()
	events := 0
	value, err := serviceFor(server).Wait(context.Background(), serviceJobID, WaitOptions{PollInterval: time.Millisecond, Timeout: time.Second, OnEvent: func(map[string]any) { events++ }})
	if err != nil {
		t.Fatal(err)
	}
	if value["status"] != "completed" || events < 3 {
		t.Fatalf("value=%v events=%d", value, events)
	}
}

func TestWaitTerminalErrors(t *testing.T) {
	for _, test := range []struct{ status, code string }{{"failed", "job_failed"}, {"cancelled", "job_cancelled"}} {
		t.Run(test.status, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_ = json.NewEncoder(w).Encode(map[string]any{"status": test.status, "message": "terminal"})
			}))
			defer server.Close()
			_, err := serviceFor(server).Wait(context.Background(), serviceJobID, WaitOptions{Timeout: time.Second})
			typed, ok := err.(*contract.CLIError)
			if !ok || typed.Code != test.code {
				t.Fatalf("unexpected %v", err)
			}
		})
	}
}

func TestWaitTimeoutDoesNotCancel(t *testing.T) {
	var posts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			posts.Add(1)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "running"})
	}))
	defer server.Close()
	_, err := serviceFor(server).Wait(context.Background(), serviceJobID, WaitOptions{Timeout: 5 * time.Millisecond, PollInterval: time.Millisecond})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "timeout" {
		t.Fatalf("unexpected %v", err)
	}
	if posts.Load() != 0 {
		t.Fatal("wait cancelled remote job")
	}
}

func TestGenerateAddsExplicitProfileIdentity(t *testing.T) {
	var received map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/capabilities" {
			_ = json.NewEncoder(w).Encode(map[string]any{"profiles": []any{map[string]any{"id": "profile-1", "version": "2", "manifest_sha256": "digest"}}})
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&received)
		w.WriteHeader(202)
		_ = json.NewEncoder(w).Encode(map[string]any{"job_id": serviceJobID})
	}))
	defer server.Close()
	_, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "image", "profile_id": "profile-1", "prompt": "hello"})
	if err != nil {
		t.Fatal(err)
	}
	if received["profile_version"] != "2" || received["profile_digest"] != "digest" {
		t.Fatalf("missing identity: %v", received)
	}
}

func TestDeriveFramePayload(t *testing.T) {
	var received map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&received)
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": serviceMediaID})
	}))
	defer server.Close()
	_, err := serviceFor(server).Derive(context.Background(), "job:"+serviceJobID+"#2", map[string]any{"operation": "frame", "position": "last"})
	if err != nil {
		t.Fatal(err)
	}
	source := received["source"].(map[string]any)
	if source["type"] != "job" || source["job_id"] != serviceJobID || source["index"] != float64(2) {
		t.Fatalf("unexpected payload: %v", received)
	}
}

func TestGenerateRetriesAmbiguousAcceptedResponseWithSamePayload(t *testing.T) {
	var calls int
	var bodies [][]byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		bodies = append(bodies, raw)
		calls++
		w.WriteHeader(202)
		if calls == 1 {
			_, _ = io.WriteString(w, "{")
			return
		}
		_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","idempotent_replay":true}`)
	}))
	defer server.Close()
	value, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "video", "prompt": "x", "director_mode": "t2v", "request_id": serviceReqID})
	if err != nil {
		t.Fatal(err)
	}
	if value["job_id"] != serviceJobID || calls != 2 || string(bodies[0]) != string(bodies[1]) {
		t.Fatalf("calls=%d bodies=%q value=%v", calls, bodies, value)
	}
}

func TestGenerateRetriesEmptyAndInvalidJobReceiptsWithSameRequest(t *testing.T) {
	var calls int
	var bodies [][]byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		bodies = append(bodies, raw)
		calls++
		w.WriteHeader(http.StatusAccepted)
		switch calls {
		case 1:
			_, _ = io.WriteString(w, `{}`)
		case 2:
			_, _ = io.WriteString(w, `{"job_id":"not-a-server-id"}`)
		default:
			_, _ = io.WriteString(w, `{"id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
		}
	}))
	defer server.Close()
	value, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "image", "prompt": "x", "request_id": serviceReqID})
	if err != nil || value["job_id"] != serviceJobID || calls != 3 {
		t.Fatalf("calls=%d value=%v err=%v", calls, value, err)
	}
	if string(bodies[0]) != string(bodies[1]) || string(bodies[1]) != string(bodies[2]) {
		t.Fatalf("submission payload changed: %q", bodies)
	}
}

func TestGenerateInvalidJobReceiptExhaustionKeepsRecoveryHandle(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(w, `{"job_id":"INVALID"}`)
	}))
	defer server.Close()
	_, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "image", "prompt": "x", "request_id": serviceReqID})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "submission_recovery_failed" || typed.Details.(map[string]any)["request_id"] != serviceReqID {
		t.Fatalf("err=%#v", err)
	}
}

func TestGenerateRejectsInvalidOrConflictingCandidateIDsAcrossAllRetries(t *testing.T) {
	otherJobID := strings.Repeat("e", 32)
	for _, response := range []string{
		fmt.Sprintf(`{"job_id":"INVALID","id":%q}`, serviceJobID),
		fmt.Sprintf(`{"job_id":%q,"id":%q}`, serviceJobID, otherJobID),
	} {
		t.Run(response, func(t *testing.T) {
			var calls int
			var bodies [][]byte
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				raw, _ := io.ReadAll(r.Body)
				bodies = append(bodies, raw)
				calls++
				w.WriteHeader(http.StatusAccepted)
				_, _ = io.WriteString(w, response)
			}))
			defer server.Close()
			_, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "image", "prompt": "x", "request_id": serviceReqID})
			typed, ok := err.(*contract.CLIError)
			if !ok || typed.Code != "submission_recovery_failed" || typed.Details.(map[string]any)["request_id"] != serviceReqID || calls != 3 {
				t.Fatalf("calls=%d err=%#v", calls, err)
			}
			if string(bodies[0]) != string(bodies[1]) || string(bodies[1]) != string(bodies[2]) {
				t.Fatalf("payload changed across retries: %q", bodies)
			}
		})
	}
}

func TestRequireResponseIDChecksEveryPresentCandidate(t *testing.T) {
	for _, value := range []map[string]any{
		{"job_id": serviceJobID, "id": ""},
		{"job_id": serviceJobID, "id": nil},
		{"job_id": serviceJobID, "id": strings.Repeat("e", 32)},
	} {
		if _, err := RequireResponseID(value, "job_id", "id"); err == nil {
			t.Fatalf("accepted candidates %#v", value)
		}
	}
	for _, value := range []map[string]any{
		{"id": serviceJobID},
		{"job_id": serviceJobID, "id": serviceJobID},
	} {
		id, err := RequireResponseID(value, "job_id", "id")
		if err != nil || id != serviceJobID {
			t.Fatalf("value=%#v id=%q err=%v", value, id, err)
		}
	}
}

func TestGenerateRecoversWhenAcceptedConnectionDropsBeforeReceipt(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		if calls.Add(1) == 1 {
			hijacker, ok := w.(http.Hijacker)
			if !ok {
				t.Fatal("response writer cannot hijack")
			}
			connection, _, err := hijacker.Hijack()
			if err != nil {
				t.Fatal(err)
			}
			_ = connection.(*net.TCPConn).SetLinger(0)
			_ = connection.Close()
			return
		}
		w.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","idempotent_replay":true}`)
	}))
	defer server.Close()
	value, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "video", "prompt": "x", "director_mode": "t2v", "request_id": serviceReqID})
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 || value["job_id"] != serviceJobID {
		t.Fatalf("calls=%d value=%v", calls.Load(), value)
	}
}

func TestGenerateRetryExhaustionIncludesRequestID(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(202); _, _ = io.WriteString(w, "{") }))
	defer server.Close()
	_, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "image", "prompt": "x", "request_id": serviceReqID})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "submission_recovery_failed" {
		t.Fatalf("err=%#v", err)
	}
	details := typed.Details.(map[string]any)
	if details["request_id"] != serviceReqID {
		t.Fatalf("details=%v", details)
	}
}

func TestGenerateRetriesRetryableHTTPAndAllSubmissionErrorsKeepRequestID(t *testing.T) {
	for _, status := range []int{http.StatusRequestTimeout, http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		var calls atomic.Int32
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if calls.Add(1) == 1 {
				w.WriteHeader(status)
				_, _ = io.WriteString(w, `{"error":{"code":"temporary","message":"retry"}}`)
				return
			}
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
		}))
		value, err := serviceFor(server).Generate(context.Background(), map[string]any{"output_type": "video", "prompt": "x", "director_mode": "t2v", "request_id": serviceReqID})
		server.Close()
		if err != nil || calls.Load() != 2 || value["job_id"] != serviceJobID {
			t.Fatalf("status=%d calls=%d value=%v err=%v", status, calls.Load(), value, err)
		}
	}

	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = io.WriteString(w, `{"error":{"code":"invalid","message":"bad"}}`)
	}))
	defer bad.Close()
	_, err := serviceFor(bad).Generate(context.Background(), map[string]any{"output_type": "video", "prompt": "x", "director_mode": "t2v", "request_id": serviceReqID})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Details.(map[string]any)["request_id"] != serviceReqID {
		t.Fatalf("err=%#v", err)
	}
	_, err = serviceFor(bad).Generate(context.Background(), map[string]any{"output_type": "video", "prompt": "x", "director_mode": "t2v"})
	typed, ok = err.(*contract.CLIError)
	automatic := ""
	if ok {
		automatic, _ = typed.Details.(map[string]any)["request_id"].(string)
	}
	if !ok || len(automatic) != 32 {
		t.Fatalf("auto request_id missing from err=%#v", err)
	}
}

func TestPrepareGenerationInjectsAndOrdersDirectorSourceReference(t *testing.T) {
	service := &Service{Context: "test"}
	for _, input := range []map[string]any{
		{"output_type": "video", "prompt": "x", "director_mode": "v2v", "source_asset_id": "c" + strings.Repeat("c", 31)},
		{"output_type": "video", "prompt": "x", "director_mode": "rv2v", "source_asset_id": strings.Repeat("c", 32), "references": []any{map[string]any{"asset_id": strings.Repeat("a", 32), "role": "identity"}, map[string]any{"asset_id": strings.Repeat("c", 32), "role": "camera"}}},
	} {
		prepared, _, err := service.PrepareGeneration(context.Background(), input)
		if err != nil {
			t.Fatal(err)
		}
		refs := prepared["references"].([]any)
		first := refs[0].(map[string]any)
		if first["asset_id"] != strings.Repeat("c", 32) || first["role"] != "motion" || first["reference_index"] != 0 {
			t.Fatalf("refs=%v", refs)
		}
		if len(refs) > 1 && refs[1].(map[string]any)["reference_index"] != 1 {
			t.Fatalf("refs=%v", refs)
		}
	}
}

func TestWaitRejectsNegativeAndUnknownStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "mystery"})
	}))
	defer server.Close()
	service := serviceFor(server)
	if _, err := service.Wait(context.Background(), serviceJobID, WaitOptions{Timeout: -time.Second}); err == nil {
		t.Fatal("negative timeout accepted")
	}
	_, err := service.Wait(context.Background(), serviceJobID, WaitOptions{Timeout: time.Second})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "invalid_response" {
		t.Fatalf("err=%#v", err)
	}
}

func TestWaitCancelledContextIsInterrupted(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "running"})
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := serviceFor(server).Wait(ctx, serviceJobID, WaitOptions{})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "interrupted" || typed.Details.(map[string]any)["job_id"] != serviceJobID {
		t.Fatalf("err=%#v", err)
	}
}

func TestMediaLocatorMaterializesInternalAsset(t *testing.T) {
	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(201)
		_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
	}))
	defer server.Close()
	reference, _, err := serviceFor(server).ResolveAsset(context.Background(), "media:"+serviceMediaID, "reference")
	if err != nil {
		t.Fatal(err)
	}
	if body["visibility"] != "internal" || reference["asset_id"] != serviceAssetID {
		t.Fatalf("body=%v reference=%v", body, reference)
	}
}

func TestInvalidUploadAndMaterializationIDsStopBeforeGeneration(t *testing.T) {
	file := filepath.Join(t.TempDir(), "source.png")
	if err := os.WriteFile(file, []byte("image"), 0o600); err != nil {
		t.Fatal(err)
	}
	var uploads, generations atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/assets":
			uploads.Add(1)
			w.WriteHeader(http.StatusCreated)
			_, _ = io.WriteString(w, `{"asset_id":"video1"}`)
		case "/api/generate":
			generations.Add(1)
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
		}
	}))
	defer server.Close()
	_, err := serviceFor(server).Generate(context.Background(), map[string]any{
		"output_type": "image", "prompt": "x", "references": []any{map[string]any{"source": file}},
	})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "invalid_response" || uploads.Load() != 1 || generations.Load() != 0 {
		t.Fatalf("uploads=%d generations=%d err=%#v", uploads.Load(), generations.Load(), err)
	}
}

func TestDeriveRejectsInvalidReceiptID(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"receipt_id":"receipt-1"}`)
	}))
	defer server.Close()
	_, err := serviceFor(server).Derive(context.Background(), "asset:"+serviceAssetID, map[string]any{"operation": "frame", "position": "first"})
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "invalid_response" {
		t.Fatalf("err=%#v", err)
	}
}

func TestMaterializationRejectsInvalidAssetID(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"asset_id":"asset-1"}`)
	}))
	defer server.Close()
	_, _, err := serviceFor(server).ResolveAsset(context.Background(), "job:"+serviceJobID, "reference")
	typed, ok := err.(*contract.CLIError)
	if !ok || typed.Code != "invalid_response" {
		t.Fatalf("err=%#v", err)
	}
}
