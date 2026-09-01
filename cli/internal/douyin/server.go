package douyin

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const APIVersion = "h3ctl.douyin/v1"

type TaskError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

type Task struct {
	ID          string          `json:"id"`
	Status      string          `json:"status"`
	SourceURL   string          `json:"source_url"`
	CreatedAt   time.Time       `json:"created_at"`
	UpdatedAt   time.Time       `json:"updated_at"`
	ExpiresAt   *time.Time      `json:"expires_at,omitempty"`
	Metadata    *Metadata       `json:"metadata,omitempty"`
	Download    *DownloadResult `json:"download,omitempty"`
	DownloadURL string          `json:"download_url,omitempty"`
	Error       *TaskError      `json:"error,omitempty"`
	filePath    string
	token       string
}

type APIConfig struct {
	DataDir       string
	TTL           time.Duration
	RateLimit     int
	MaxConcurrent int
	Now           func() time.Time
}

type API struct {
	extractor Extractor
	dataDir   string
	ttl       time.Duration
	now       func() time.Time
	ctx       context.Context
	cancel    context.CancelFunc
	sem       chan struct{}

	mu      sync.RWMutex
	tasks   map[string]*Task
	byURL   map[string]string
	byToken map[string]string
	limits  map[string]*rateWindow
	limit   int
}

type rateWindow struct {
	started time.Time
	count   int
}

func DefaultDataDir() (string, error) {
	root, err := os.UserCacheDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "h3ctl", "douyin"), nil
}

func NewAPI(extractor Extractor, config APIConfig) (*API, error) {
	if extractor == nil {
		return nil, errors.New("extractor is required")
	}
	if config.DataDir == "" {
		var err error
		config.DataDir, err = DefaultDataDir()
		if err != nil {
			return nil, err
		}
	}
	abs, err := filepath.Abs(config.DataDir)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Join(abs, "downloads"), 0o700); err != nil {
		return nil, err
	}
	if config.TTL <= 0 {
		config.TTL = time.Hour
	}
	if config.RateLimit <= 0 {
		config.RateLimit = 30
	}
	if config.MaxConcurrent <= 0 {
		config.MaxConcurrent = 2
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	ctx, cancel := context.WithCancel(context.Background())
	api := &API{
		extractor: extractor, dataDir: abs, ttl: config.TTL, now: config.Now,
		ctx: ctx, cancel: cancel, sem: make(chan struct{}, config.MaxConcurrent),
		tasks: map[string]*Task{}, byURL: map[string]string{}, byToken: map[string]string{},
		limits: map[string]*rateWindow{}, limit: config.RateLimit,
	}
	go api.cleanupLoop()
	return api, nil
}

func (a *API) Close() { a.cancel() }

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", a.health)
	mux.HandleFunc("POST /api/parse", a.parse)
	mux.HandleFunc("GET /api/tasks/{id}", a.getTask)
	mux.HandleFunc("GET /api/download/{token}", a.download)
	mux.HandleFunc("GET /openapi.json", a.openapi)
	mux.HandleFunc("GET /docs", a.docs)
	return securityHeaders(mux)
}

func (a *API) Serve(ctx context.Context, listener net.Listener) error {
	server := &http.Server{Handler: a.Handler(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
		return nil
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

func ValidateListenAddress(value string) error {
	host, port, err := net.SplitHostPort(value)
	if err != nil {
		return fmt.Errorf("invalid listen address: %w", err)
	}
	if port == "" {
		return errors.New("listen address requires a port")
	}
	if host == "localhost" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return errors.New("Douyin API may only listen on a loopback address")
	}
	return nil
}

func (a *API) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "api_version": APIVersion})
}

func (a *API) parse(w http.ResponseWriter, request *http.Request) {
	if !a.allow(request) {
		writeAPIError(w, http.StatusTooManyRequests, "rate_limited", "too many parse requests")
		return
	}
	request.Body = http.MaxBytesReader(w, request.Body, 64<<10)
	decoder := json.NewDecoder(request.Body)
	decoder.DisallowUnknownFields()
	var body struct {
		Text string `json:"text"`
	}
	if err := decoder.Decode(&body); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid_request", "body must be JSON with a text field")
		return
	}
	if err := ensureJSONEOF(decoder); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid_request", "body must contain one JSON object")
		return
	}
	link, err := ExtractURL(body.Text)
	if err != nil {
		typed := MapError(err)
		writeAPIError(w, http.StatusBadRequest, typed.Code, typed.Message)
		return
	}
	task, reused, err := a.start(link)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "task_create_failed", err.Error())
		return
	}
	status := http.StatusAccepted
	if reused {
		status = http.StatusOK
	}
	writeJSON(w, status, map[string]any{"task": publicTask(task), "reused": reused})
}

func (a *API) getTask(w http.ResponseWriter, request *http.Request) {
	id := request.PathValue("id")
	a.mu.RLock()
	task := cloneTask(a.tasks[id])
	a.mu.RUnlock()
	if task == nil {
		writeAPIError(w, http.StatusNotFound, "not_found", "task not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": publicTask(task)})
}

func (a *API) download(w http.ResponseWriter, request *http.Request) {
	token := request.PathValue("token")
	a.mu.RLock()
	id := a.byToken[token]
	task := cloneTask(a.tasks[id])
	a.mu.RUnlock()
	if task == nil || task.Status != "completed" || task.filePath == "" || !expiresAfter(task, a.now()) {
		writeAPIError(w, http.StatusNotFound, "not_found", "download token is invalid or expired")
		return
	}
	if !within(filepath.Join(a.dataDir, "downloads"), task.filePath) {
		writeAPIError(w, http.StatusInternalServerError, "unsafe_path", "download path failed validation")
		return
	}
	file, err := os.Open(task.filePath)
	if err != nil {
		writeAPIError(w, http.StatusNotFound, "not_found", "download file is unavailable")
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		writeAPIError(w, http.StatusNotFound, "not_found", "download file is unavailable")
		return
	}
	name := filepath.Base(task.filePath)
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename*=UTF-8''%s", urlPathEscape(name)))
	if contentType := mime.TypeByExtension(filepath.Ext(name)); contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	http.ServeContent(w, request, name, info.ModTime(), file)
}

func (a *API) start(link string) (*Task, bool, error) {
	now := a.now()
	a.mu.Lock()
	if existingID := a.byURL[link]; existingID != "" {
		if existing := a.tasks[existingID]; existing != nil && (existing.Status == "pending" || existing.Status == "running" || expiresAfter(existing, now)) {
			value := cloneTask(existing)
			a.mu.Unlock()
			return value, true, nil
		}
	}
	id, err := randomHex(16)
	if err != nil {
		a.mu.Unlock()
		return nil, false, err
	}
	task := &Task{ID: id, Status: "pending", SourceURL: link, CreatedAt: now, UpdatedAt: now}
	a.tasks[id], a.byURL[link] = task, id
	value := cloneTask(task)
	a.mu.Unlock()
	go a.runTask(id, link)
	return value, false, nil
}

func (a *API) runTask(id, link string) {
	select {
	case a.sem <- struct{}{}:
		defer func() { <-a.sem }()
	case <-a.ctx.Done():
		return
	}
	a.updateTask(id, func(task *Task) { task.Status, task.UpdatedAt = "running", a.now() })
	metadata, err := a.extractor.Parse(a.ctx, link)
	if err != nil {
		a.failTask(id, err)
		return
	}
	template := filepath.Join(a.dataDir, "downloads", id+".%(ext)s")
	result, err := a.extractor.Download(a.ctx, link, template, false)
	if err != nil {
		a.failTask(id, err)
		return
	}
	if !within(filepath.Join(a.dataDir, "downloads"), result.Path) {
		a.failTask(id, &Error{Code: "unsafe_path", Message: "extractor returned a path outside the download cache"})
		return
	}
	token, err := randomHex(24)
	if err != nil {
		a.failTask(id, err)
		return
	}
	a.updateTask(id, func(task *Task) {
		now := a.now()
		expiresAt := now.Add(a.ttl)
		task.Status, task.UpdatedAt, task.ExpiresAt = "completed", now, &expiresAt
		task.Metadata, task.Download = &metadata, &result
		task.filePath, task.token, task.DownloadURL = result.Path, token, "/api/download/"+token
		a.byToken[token] = id
	})
}

func (a *API) failTask(id string, err error) {
	typed := MapError(err)
	a.updateTask(id, func(task *Task) {
		now := a.now()
		expiresAt := now.Add(a.ttl)
		task.Status, task.UpdatedAt, task.ExpiresAt = "failed", now, &expiresAt
		task.Error = &TaskError{Code: typed.Code, Message: typed.Message, Retryable: typed.Retryable}
	})
}

func (a *API) updateTask(id string, update func(*Task)) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if task := a.tasks[id]; task != nil {
		update(task)
	}
}

func (a *API) cleanupLoop() {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-a.ctx.Done():
			return
		case <-ticker.C:
			a.cleanupExpired()
		}
	}
}

func (a *API) cleanupExpired() {
	now := a.now()
	paths := []string{}
	a.mu.Lock()
	for id, task := range a.tasks {
		if (task.Status == "completed" || task.Status == "failed") && !expiresAfter(task, now) {
			delete(a.tasks, id)
			delete(a.byURL, task.SourceURL)
			delete(a.byToken, task.token)
			if task.filePath != "" && within(filepath.Join(a.dataDir, "downloads"), task.filePath) {
				paths = append(paths, task.filePath)
			}
		}
	}
	for ip, window := range a.limits {
		if now.Sub(window.started) > 2*time.Minute {
			delete(a.limits, ip)
		}
	}
	a.mu.Unlock()
	for _, path := range paths {
		_ = os.Remove(path)
	}
}

func (a *API) allow(request *http.Request) bool {
	host, _, err := net.SplitHostPort(request.RemoteAddr)
	if err != nil {
		host = request.RemoteAddr
	}
	now := a.now()
	a.mu.Lock()
	defer a.mu.Unlock()
	window := a.limits[host]
	if window == nil || now.Sub(window.started) >= time.Minute {
		a.limits[host] = &rateWindow{started: now, count: 1}
		return true
	}
	if window.count >= a.limit {
		return false
	}
	window.count++
	return true
}

func (a *API) docs(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, swaggerHTML)
}

func (a *API) openapi(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, openAPISpec())
}

func openAPISpec() map[string]any {
	jsonContent := func(ref string) map[string]any {
		return map[string]any{"application/json": map[string]any{"schema": map[string]any{"$ref": ref}}}
	}
	response := func(description, ref string) map[string]any {
		value := map[string]any{"description": description}
		if ref != "" {
			value["content"] = jsonContent(ref)
		}
		return value
	}
	return map[string]any{
		"openapi": "3.1.0",
		"info":    map[string]any{"title": "h3ctl Douyin Download API", "version": APIVersion, "description": "Local-only Douyin parsing and cached download API backed by yt-dlp."},
		"paths": map[string]any{
			"/health": map[string]any{"get": map[string]any{
				"summary":   "Health check",
				"responses": map[string]any{"200": response("Healthy", "#/components/schemas/HealthResponse")},
			}},
			"/api/parse": map[string]any{"post": map[string]any{
				"summary":     "Submit a Douyin parse and download task",
				"requestBody": map[string]any{"required": true, "content": jsonContent("#/components/schemas/ParseRequest")},
				"responses": map[string]any{
					"200": response("Unexpired task reused", "#/components/schemas/ParseResponse"),
					"202": response("New task accepted", "#/components/schemas/ParseResponse"),
					"400": response("Invalid request", "#/components/schemas/ErrorResponse"),
					"429": response("Rate limited", "#/components/schemas/ErrorResponse"),
				},
			}},
			"/api/tasks/{id}": map[string]any{"get": map[string]any{
				"summary":    "Get task status",
				"parameters": []any{map[string]any{"name": "id", "in": "path", "required": true, "schema": map[string]any{"type": "string"}}},
				"responses": map[string]any{
					"200": response("Current task state", "#/components/schemas/TaskResponse"),
					"404": response("Task not found", "#/components/schemas/ErrorResponse"),
				},
			}},
			"/api/download/{token}": map[string]any{"get": map[string]any{
				"summary":    "Download completed media (HTTP Range supported)",
				"parameters": []any{map[string]any{"name": "token", "in": "path", "required": true, "schema": map[string]any{"type": "string"}}},
				"responses": map[string]any{
					"200": map[string]any{"description": "Complete media file", "content": map[string]any{"video/mp4": map[string]any{"schema": map[string]any{"type": "string", "format": "binary"}}}},
					"206": map[string]any{"description": "Requested media byte range"},
					"404": response("Invalid or expired token", "#/components/schemas/ErrorResponse"),
				},
			}},
		},
		"components": map[string]any{"schemas": map[string]any{
			"ParseRequest": map[string]any{
				"type": "object", "additionalProperties": false, "required": []string{"text"},
				"properties": map[string]any{"text": map[string]any{"type": "string", "description": "Douyin share text or HTTPS URL"}},
			},
			"ParseResponse": map[string]any{
				"type": "object", "required": []string{"task", "reused"},
				"properties": map[string]any{"task": map[string]any{"$ref": "#/components/schemas/Task"}, "reused": map[string]any{"type": "boolean"}},
			},
			"TaskResponse": map[string]any{
				"type": "object", "required": []string{"task"},
				"properties": map[string]any{"task": map[string]any{"$ref": "#/components/schemas/Task"}},
			},
			"Task": map[string]any{
				"type": "object", "required": []string{"id", "status", "source_url", "created_at", "updated_at"},
				"properties": map[string]any{
					"id": map[string]any{"type": "string"}, "status": map[string]any{"type": "string", "enum": []string{"pending", "running", "completed", "failed"}},
					"source_url": map[string]any{"type": "string", "format": "uri"}, "created_at": map[string]any{"type": "string", "format": "date-time"},
					"updated_at": map[string]any{"type": "string", "format": "date-time"}, "expires_at": map[string]any{"type": "string", "format": "date-time"},
					"metadata": map[string]any{"$ref": "#/components/schemas/Metadata"}, "download": map[string]any{"$ref": "#/components/schemas/DownloadResult"},
					"download_url": map[string]any{"type": "string"}, "error": map[string]any{"$ref": "#/components/schemas/TaskError"},
				},
			},
			"Metadata": map[string]any{
				"type": "object", "required": []string{"id"},
				"properties": map[string]any{
					"id": map[string]any{"type": "string"}, "title": map[string]any{"type": "string"}, "uploader": map[string]any{"type": "string"},
					"duration": map[string]any{"type": "number"}, "thumbnail": map[string]any{"type": "string", "format": "uri"},
					"webpage_url": map[string]any{"type": "string", "format": "uri"}, "width": map[string]any{"type": "integer"},
					"height": map[string]any{"type": "integer"}, "ext": map[string]any{"type": "string"},
					"formats": map[string]any{"type": "array", "items": map[string]any{"type": "object"}},
				},
			},
			"DownloadResult": map[string]any{
				"type": "object", "required": []string{"size"},
				"properties": map[string]any{"size": map[string]any{"type": "integer", "format": "int64"}, "sha256": map[string]any{"type": "string"}},
			},
			"TaskError": map[string]any{
				"type": "object", "required": []string{"code", "message", "retryable"},
				"properties": map[string]any{"code": map[string]any{"type": "string"}, "message": map[string]any{"type": "string"}, "retryable": map[string]any{"type": "boolean"}},
			},
			"ErrorResponse": map[string]any{
				"type": "object", "required": []string{"error"},
				"properties": map[string]any{"error": map[string]any{"type": "object", "required": []string{"code", "message"}, "properties": map[string]any{"code": map[string]any{"type": "string"}, "message": map[string]any{"type": "string"}}}},
			},
			"HealthResponse": map[string]any{
				"type": "object", "required": []string{"status", "api_version"},
				"properties": map[string]any{"status": map[string]any{"type": "string", "const": "ok"}, "api_version": map[string]any{"type": "string"}},
			},
		},
		},
	}
}

func publicTask(task *Task) *Task {
	value := cloneTask(task)
	if value != nil && value.Download != nil {
		value.Download.Path = ""
	}
	return value
}

func cloneTask(task *Task) *Task {
	if task == nil {
		return nil
	}
	value := *task
	if task.ExpiresAt != nil {
		expiresAt := *task.ExpiresAt
		value.ExpiresAt = &expiresAt
	}
	if task.Metadata != nil {
		metadata := *task.Metadata
		metadata.Formats = append([]Format(nil), task.Metadata.Formats...)
		value.Metadata = &metadata
	}
	if task.Download != nil {
		download := *task.Download
		value.Download = &download
	}
	if task.Error != nil {
		taskError := *task.Error
		value.Error = &taskError
	}
	return &value
}

func expiresAfter(task *Task, now time.Time) bool {
	return task != nil && task.ExpiresAt != nil && task.ExpiresAt.After(now)
}

func ensureJSONEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON")
	}
	return nil
}

func randomHex(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return hex.EncodeToString(value), nil
}

func within(root, path string) bool {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return false
	}
	pathAbs, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	relative, err := filepath.Rel(rootAbs, pathAbs)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func urlPathEscape(value string) string {
	return url.PathEscape(value)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeAPIError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"error": map[string]any{"code": code, "message": message}})
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, request)
	})
}

const swaggerHTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>h3ctl Douyin API</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',deepLinking:true});</script>
</body></html>`
