package contract

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const SchemaVersion = "h3ctl.output/v1"

type Envelope struct {
	SchemaVersion string        `json:"schema_version"`
	OK            bool          `json:"ok"`
	Command       string        `json:"command"`
	Data          any           `json:"data,omitempty"`
	Warnings      []string      `json:"warnings"`
	Error         *ErrorPayload `json:"error,omitempty"`
}

type ErrorPayload struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
	Status    int    `json:"status,omitempty"`
	Details   any    `json:"details,omitempty"`
}

type CLIError struct {
	Code      string
	Message   string
	Retryable bool
	Status    int
	Details   any
	Cause     error
}

func (e *CLIError) Error() string { return e.Message }
func (e *CLIError) Unwrap() error { return e.Cause }

func NewError(code, message string) *CLIError { return &CLIError{Code: code, Message: message} }

func ExitCode(err error) int {
	var typed *CLIError
	if !errors.As(err, &typed) {
		return 1
	}
	if typed.Status == 404 {
		return 4
	}
	switch typed.Code {
	case "usage", "invalid_argument", "invalid_locator", "invalid_spec":
		return 2
	case "unauthorized", "forbidden":
		return 3
	case "not_found":
		return 4
	case "timeout":
		return 5
	case "job_failed":
		return 6
	case "job_cancelled":
		return 7
	case "unsupported":
		return 8
	default:
		return 1
	}
}

func ErrorEnvelope(command string, err error) Envelope {
	value := &CLIError{Code: "internal_error", Message: err.Error()}
	var typed *CLIError
	if errors.As(err, &typed) {
		value = typed
	}
	return Envelope{SchemaVersion: SchemaVersion, OK: false, Command: command, Warnings: []string{}, Error: &ErrorPayload{
		Code: value.Code, Message: value.Message, Retryable: value.Retryable, Status: value.Status, Details: value.Details,
	}}
}

func Success(command string, data any) Envelope {
	return Envelope{SchemaVersion: SchemaVersion, OK: true, Command: command, Data: data, Warnings: []string{}}
}

func WriteJSON(w io.Writer, value any) error {
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}

func Unsupported(command, reason string) error {
	return &CLIError{Code: "unsupported", Message: fmt.Sprintf("%s is not supported by the current H3 Studio API: %s", command, reason)}
}
