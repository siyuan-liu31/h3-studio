package contract

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestEnvelopeAlwaysHasStableFields(t *testing.T) {
	var out bytes.Buffer
	if err := WriteJSON(&out, Success("job.get", map[string]any{"id": "j"})); err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err := json.Unmarshal(out.Bytes(), &value); err != nil {
		t.Fatal(err)
	}
	if value["schema_version"] != SchemaVersion || value["ok"] != true {
		t.Fatalf("value=%v", value)
	}
	warnings, ok := value["warnings"].([]any)
	if !ok || len(warnings) != 0 {
		t.Fatalf("warnings=%#v", value["warnings"])
	}
}
func TestStableExitCodes(t *testing.T) {
	tests := map[string]int{"usage": 2, "unauthorized": 3, "not_found": 4, "timeout": 5, "job_failed": 6, "job_cancelled": 7, "unsupported": 8}
	for code, want := range tests {
		if got := ExitCode(NewError(code, "x")); got != want {
			t.Errorf("%s: got %d want %d", code, got, want)
		}
	}
}
func TestHTTP404AlwaysUsesMissingExitCode(t *testing.T) {
	if got := ExitCode(&CLIError{Code: "asset_missing", Status: 404}); got != 4 {
		t.Fatalf("got %d", got)
	}
}
