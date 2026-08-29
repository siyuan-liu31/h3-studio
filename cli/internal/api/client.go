package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"h3studio/cli/internal/contract"
)

type Client struct {
	BaseURL         string
	HTTP            *http.Client
	ControlTimeout  time.Duration
	TransferTimeout time.Duration
	MediaTimeout    time.Duration
}

func New(baseURL string, timeout time.Duration) *Client {
	return NewWithTimeouts(baseURL, timeout, 0, 0)
}

func NewWithTimeouts(baseURL string, control, transfer, media time.Duration) *Client {
	return &Client{
		BaseURL:        strings.TrimRight(baseURL, "/"),
		HTTP:           &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }},
		ControlTimeout: control, TransferTimeout: transfer, MediaTimeout: media,
	}
}

func (c *Client) request(ctx context.Context, method, path string, body io.Reader, contentType string, contentLength int64, timeout time.Duration) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if contentLength >= 0 {
		req.ContentLength = contentLength
	}
	client := *c.HTTP
	// Keep this invariant even when tests or embedders replace HTTP. Go's
	// Redirects are deliberately not followed: every request stays on the
	// configured H3 Studio origin.
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	client.Timeout = timeout
	response, err := client.Do(req)
	if err != nil {
		return nil, &contract.CLIError{Code: "network_error", Message: err.Error(), Retryable: true, Cause: err}
	}
	return response, nil
}

func decodeResponse(response *http.Response, destination any) error {
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 2<<20))
		var parsed struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
				Details any    `json:"details"`
			} `json:"error"`
			Code    string `json:"code"`
			Message string `json:"message"`
			Details any    `json:"details"`
		}
		_ = json.Unmarshal(raw, &parsed)
		code, message, details := parsed.Error.Code, parsed.Error.Message, parsed.Error.Details
		if code == "" {
			code, message, details = parsed.Code, parsed.Message, parsed.Details
		}
		if code == "" {
			code = http.StatusText(response.StatusCode)
			code = strings.ToLower(strings.ReplaceAll(code, " ", "_"))
		}
		if message == "" {
			message = strings.TrimSpace(string(raw))
			if message == "" {
				message = response.Status
			}
		}
		if response.StatusCode == http.StatusUnauthorized {
			code = "unauthorized"
		}
		if response.StatusCode == http.StatusForbidden {
			code = "forbidden"
		}
		if response.StatusCode == http.StatusNotFound && code == "not_found" {
			code = "not_found"
		}
		return &contract.CLIError{Code: code, Message: message, Status: response.StatusCode, Details: details, Retryable: response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode >= 500}
	}
	if destination == nil {
		_, err := io.Copy(io.Discard, response.Body)
		return err
	}
	if err := json.NewDecoder(response.Body).Decode(destination); err != nil {
		return &contract.CLIError{Code: "invalid_response", Message: "server returned invalid JSON: " + err.Error(), Cause: err}
	}
	return nil
}

func (c *Client) JSON(ctx context.Context, method, path string, body any, destination any) error {
	_, err := c.JSONStatus(ctx, method, path, body, destination)
	return err
}

// JSONStatus performs a control-plane JSON request and returns the HTTP status
// so callers with a strict creation contract can reject an unexpected 2xx.
func (c *Client) JSONStatus(ctx context.Context, method, path string, body any, destination any) (int, error) {
	var input io.Reader
	var length int64
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		input, length = bytes.NewReader(raw), int64(len(raw))
	} else {
		length = 0
	}
	response, err := c.request(ctx, method, path, input, "application/json", length, c.ControlTimeout)
	if err != nil {
		return 0, err
	}
	status := response.StatusCode
	return status, decodeResponse(response, destination)
}

func (c *Client) JSONMedia(ctx context.Context, method, path string, body any, destination any) error {
	var input io.Reader
	var length int64
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		input, length = bytes.NewReader(raw), int64(len(raw))
	}
	response, err := c.request(ctx, method, path, input, "application/json", length, c.MediaTimeout)
	if err != nil {
		return err
	}
	return decodeResponse(response, destination)
}

func (c *Client) Get(ctx context.Context, path string) (map[string]any, error) {
	value := map[string]any{}
	err := c.JSON(ctx, http.MethodGet, path, nil, &value)
	return value, err
}

func (c *Client) Upload(ctx context.Context, path, kind string) (map[string]any, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	boundary := fmt.Sprintf("h3ctl-%d", time.Now().UnixNano())
	var prefix bytes.Buffer
	writer := multipart.NewWriter(&prefix)
	if err := writer.SetBoundary(boundary); err != nil {
		return nil, err
	}
	if err := writer.WriteField("kind", kind); err != nil {
		return nil, err
	}
	if _, err := writer.CreateFormFile("file", filepath.Base(path)); err != nil {
		return nil, err
	}
	suffix := []byte("\r\n--" + boundary + "--\r\n")
	length := int64(prefix.Len()) + info.Size() + int64(len(suffix))
	stream := io.MultiReader(bytes.NewReader(prefix.Bytes()), file, bytes.NewReader(suffix))
	response, err := c.request(ctx, http.MethodPost, "/api/assets", stream, "multipart/form-data; boundary="+boundary, length, c.TransferTimeout)
	if err != nil {
		return nil, err
	}
	value := map[string]any{}
	if err := decodeResponse(response, &value); err != nil {
		return nil, err
	}
	return value, nil
}

func (c *Client) Download(ctx context.Context, path, destination string, force bool) (map[string]any, error) {
	if destination == "" {
		return nil, contract.NewError("invalid_argument", "download destination is required")
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		return nil, err
	}
	response, err := c.request(ctx, http.MethodGet, path, nil, "", 0, c.TransferTimeout)
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, decodeResponse(response, nil)
	}
	defer response.Body.Close()
	output, err := os.CreateTemp(filepath.Dir(destination), "."+filepath.Base(destination)+".*.part")
	if err != nil {
		return nil, err
	}
	part := output.Name()
	if err := output.Chmod(0o600); err != nil {
		output.Close()
		os.Remove(part)
		return nil, err
	}
	written, copyErr := io.Copy(output, response.Body)
	closeErr := output.Close()
	if copyErr != nil {
		os.Remove(part)
		return nil, copyErr
	}
	if closeErr != nil {
		os.Remove(part)
		return nil, closeErr
	}
	if expected := response.Header.Get("Content-Length"); expected != "" {
		if n, parseErr := strconv.ParseInt(expected, 10, 64); parseErr == nil && n != written {
			os.Remove(part)
			return nil, contract.NewError("truncated_download", "download size did not match Content-Length")
		}
	}
	if force {
		if err := os.Rename(part, destination); err != nil {
			os.Remove(part)
			return nil, err
		}
	} else {
		// A same-directory hard link is an atomic no-replace commit. It closes
		// the Stat/Rename TOCTOU window when two agents download concurrently.
		if err := os.Link(part, destination); err != nil {
			os.Remove(part)
			if os.IsExist(err) {
				return nil, contract.NewError("destination_exists", "destination already exists; pass --force to overwrite")
			}
			return nil, err
		}
		if err := os.Remove(part); err != nil {
			return nil, err
		}
	}
	abs, _ := filepath.Abs(destination)
	return map[string]any{"path": abs, "bytes": written}, nil
}

func Query(path string, values url.Values) string {
	if encoded := values.Encode(); encoded != "" {
		return path + "?" + encoded
	}
	return path
}
