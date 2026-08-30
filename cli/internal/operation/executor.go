package operation

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/resource"
)

type Runtime struct {
	Service   *Service
	OnEvent   func(map[string]any)
	CopyAsset func(context.Context, string, string) (map[string]any, error)
}

func Execute(ctx context.Context, runtime Runtime, name string, input map[string]any) (any, error) {
	if err := ValidateInput(name, input); err != nil {
		return nil, err
	}
	s := runtime.Service
	require := func(key string) string { return stringValue(input[key], "") }
	switch name {
	case "asset.upload":
		return s.Upload(ctx, require("path"), stringValue(input["kind"], "auto"))
	case "asset.download":
		return s.API.Download(ctx, "/api/assets/"+url.PathEscape(require("asset_id"))+"/content", require("to"), boolValue(input["force"]))
	case "asset.list":
		query := url.Values{}
		if value := stringValue(input["query"], ""); value != "" {
			query.Set("q", value)
		}
		if value := stringValue(input["folder_id"], ""); value != "" {
			query.Set("folder_id", value)
		}
		return s.API.Get(ctx, api.Query("/api/assets", query))
	case "asset.copy":
		if runtime.CopyAsset == nil {
			return nil, contract.NewError("internal_error", "asset copy runtime is unavailable")
		}
		return runtime.CopyAsset(ctx, require("source"), require("to_context"))
	case "generate.image", "generate.video":
		input["output_type"] = strings.TrimPrefix(name, "generate.")
		return s.Generate(ctx, input)
	case "job.list":
		limit := intValue(input["limit"], 20)
		query := url.Values{"limit": {strconv.Itoa(limit)}}
		if value := stringValue(input["cursor"], ""); value != "" {
			query.Set("cursor", value)
		}
		if boolValue(input["results"]) {
			query.Set("results", "1")
		}
		return s.API.Get(ctx, "/api/jobs?"+query.Encode())
	case "job.get":
		return s.API.Get(ctx, "/api/jobs/"+url.PathEscape(require("job_id")))
	case "job.wait":
		return s.Wait(ctx, require("job_id"), WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
	case "job.cancel":
		return jsonAction(ctx, s, http.MethodPost, "/api/jobs/"+url.PathEscape(require("job_id"))+"/cancel", nil)
	case "job.download":
		return s.API.Download(ctx, "/api/download?id="+url.QueryEscape(require("job_id"))+"&index="+strconv.Itoa(intValue(input["index"], 0)), require("to"), boolValue(input["force"]))
	case "job.save":
		body := map[string]any{"index": intValue(input["index"], 0), "visibility": "library"}
		copyOptional(body, input, "display_name", "folder_id")
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/jobs/"+url.PathEscape(require("job_id"))+"/assets", body, "asset_id", "id")
	case "job.workflow":
		path := "/api/jobs/" + url.PathEscape(require("job_id")) + "/workflow"
		if to := stringValue(input["to"], ""); to != "" {
			return s.API.Download(ctx, path+"?download=1", to, boolValue(input["force"]))
		}
		return s.API.Get(ctx, path)
	case "job.resume":
		submitted, err := s.Resume(ctx, require("job_id"), intValue(input["additional_steps"], 0), stringValue(input["request_id"], ""))
		if err != nil {
			return nil, err
		}
		jobID := stringValue(submitted["job_id"], "")
		result := map[string]any{"submitted": submitted, "job_id": jobID, "request_id": submitted["request_id"]}
		if !boolValue(input["wait"]) && stringValue(input["download"], "") == "" {
			return result, nil
		}
		if runtime.OnEvent != nil {
			runtime.OnEvent(map[string]any{"type": "submitted", "job_id": jobID, "request_id": submitted["request_id"]})
		}
		completed, err := s.Wait(ctx, jobID, WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
		if err != nil {
			return nil, err
		}
		result["completed"] = completed
		if destination := stringValue(input["download"], ""); destination != "" {
			downloaded, err := s.API.Download(ctx, "/api/download?id="+url.QueryEscape(jobID)+"&index=0", destination, boolValue(input["force"]))
			if err != nil {
				return nil, err
			}
			result["download"] = downloaded
		}
		return result, nil
	case "job.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/jobs/"+url.PathEscape(require("job_id")), nil)
	case "media.frame":
		body := map[string]any{"operation": "frame", "position": require("position")}
		copyOptional(body, input, "time", "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.endpoints":
		first, err := s.Derive(ctx, require("source"), map[string]any{"operation": "frame", "position": "first"})
		if err != nil {
			return nil, err
		}
		last, err := s.Derive(ctx, require("source"), map[string]any{"operation": "frame", "position": "last"})
		if err != nil {
			return nil, &contract.CLIError{Code: "partial_failure", Message: err.Error(), Details: map[string]any{"first": first}, Cause: err}
		}
		return map[string]any{"first": first, "last": last}, nil
	case "media.trim":
		op := "video_trim"
		if boolValue(input["audio"]) {
			op = "audio_trim"
		}
		body := map[string]any{"operation": op, "start": input["start"], "end": input["end"]}
		copyOptional(body, input, "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.extract_audio", "media.remove_audio":
		body := map[string]any{"operation": strings.TrimPrefix(name, "media.")}
		copyOptional(body, input, "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.prepare_reference":
		body := map[string]any{
			"operation":      "prepare_h3_reference",
			"max_short_edge": intValue(input["max_short_edge"], 480),
			"max_long_edge":  intValue(input["max_long_edge"], 864),
			"fps":            intValue(input["fps"], 24),
			"max_duration":   numberValue(input["max_duration"], 15),
			"fit":            stringValue(input["fit"], "contain"),
			"alignment":      intValue(input["alignment"], 32),
			"pad_mode":       stringValue(input["pad_mode"], "edge"),
		}
		copyOptional(body, input, "preset", "audio", "display_name")
		value, err := s.DeriveWithEvents(ctx, require("source"), body, runtime.OnEvent)
		if err == nil {
			value["locator"] = "media:" + stringValue(value["id"], "")
		}
		return value, err
	case "media.list":
		return s.API.Get(ctx, "/api/derivations")
	case "media.get":
		return s.API.Get(ctx, "/api/derivations/"+url.PathEscape(require("media_id")))
	case "media.download":
		return s.API.Download(ctx, "/api/derivations/"+url.PathEscape(require("media_id"))+"/download", require("to"), boolValue(input["force"]))
	case "media.save":
		body := map[string]any{"visibility": "library"}
		copyOptional(body, input, "display_name", "folder_id")
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/derivations/"+url.PathEscape(require("media_id"))+"/assets", body, "asset_id", "id")
	case "media.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/derivations/"+url.PathEscape(require("media_id")), nil)
	case "project.create":
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/video-projects", input["spec"], "project_id", "id")
	case "project.apply":
		return jsonActionWithID(ctx, s, http.MethodPut, "/api/video-projects/"+url.PathEscape(require("project_id")), input["spec"], "project_id", "id")
	case "project.list":
		return s.API.Get(ctx, "/api/video-projects")
	case "project.get":
		return s.API.Get(ctx, "/api/video-projects/"+url.PathEscape(require("project_id")))
	case "project.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/video-projects/"+url.PathEscape(require("project_id")), nil)
	case "project.run":
		body := map[string]any{}
		copyOptional(body, input, "segment_ids")
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/run", body)
	case "project.wait":
		return s.WaitProject(ctx, require("project_id"), durationSeconds(input["timeout_seconds"]), durationSeconds(input["poll_seconds"]), runtime.OnEvent)
	case "project.stop", "project.merge":
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/"+strings.TrimPrefix(name, "project."), map[string]any{})
	case "project.rerun":
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/segments/"+url.PathEscape(require("segment_id"))+"/run", map[string]any{})
	case "project.download":
		return s.API.Download(ctx, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/merged/download", require("to"), boolValue(input["force"]))
	default:
		return nil, contract.NewError("not_found", "operation not found")
	}
}

func (s *Service) WaitProject(ctx context.Context, id string, timeout, poll time.Duration, onEvent func(map[string]any)) (map[string]any, error) {
	if !resource.ValidServerID(id) {
		return nil, contract.NewError("invalid_argument", "project_id must be 32 lowercase hex characters")
	}
	if timeout < 0 || poll < 0 {
		return nil, contract.NewError("invalid_argument", "project wait durations cannot be negative")
	}
	if poll == 0 {
		poll = s.PollInterval
	}
	if poll <= 0 {
		poll = 5 * time.Second
	}
	waitCtx := ctx
	var cancel context.CancelFunc
	if timeout > 0 {
		waitCtx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	failures := 0
	for {
		if err := waitCtx.Err(); err != nil {
			return nil, projectWaitError(id, err)
		}
		value, err := s.API.Get(waitCtx, "/api/video-projects/"+url.PathEscape(id))
		if err != nil {
			if waitErr := waitCtx.Err(); waitErr != nil {
				return nil, projectWaitError(id, waitErr)
			}
			var cliErr *contract.CLIError
			if !errors.As(err, &cliErr) || !cliErr.Retryable || failures >= 5 {
				return nil, err
			}
			failures++
			if !waitDelay(waitCtx, boundedBackoff(poll, failures)) {
				continue
			}
			continue
		}
		failures = 0
		status := stringValue(value["status"], "")
		if onEvent != nil {
			onEvent(map[string]any{"type": "project_status", "project_id": id, "status": status, "progress": value["progress"]})
		}
		switch status {
		case "completed", "merged", "partial":
			return value, nil
		case "failed":
			return nil, &contract.CLIError{Code: "job_failed", Message: "project failed", Details: value}
		case "stopped", "cancelled":
			return nil, &contract.CLIError{Code: "job_cancelled", Message: "project stopped", Details: value}
		case "draft", "running", "stopping", "merging":
		default:
			return nil, &contract.CLIError{Code: "invalid_response", Message: "server returned unknown project status", Details: map[string]any{"project_id": id, "status": status}}
		}
		waitDelay(waitCtx, poll)
	}
}

func projectWaitError(id string, err error) error {
	code, message := "interrupted", "waiting interrupted; the remote project was not stopped"
	if errors.Is(err, context.DeadlineExceeded) {
		code, message = "timeout", "timed out waiting for project; the remote project is still running"
	}
	return &contract.CLIError{Code: code, Message: message, Details: map[string]any{"project_id": id}, Cause: err}
}

func waitDelay(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func boundedBackoff(base time.Duration, failures int) time.Duration {
	delay := base * time.Duration(1<<min(failures-1, 4))
	if delay > 15*time.Second {
		return 15 * time.Second
	}
	return delay
}

func jsonAction(ctx context.Context, service *Service, method, path string, body any) (map[string]any, error) {
	value := map[string]any{}
	err := service.API.JSON(ctx, method, path, body, &value)
	return value, err
}

func jsonActionWithID(ctx context.Context, service *Service, method, path string, body any, fields ...string) (map[string]any, error) {
	value, err := jsonAction(ctx, service, method, path, body)
	if err != nil {
		return nil, err
	}
	if _, err := RequireResponseID(value, fields...); err != nil {
		return nil, err
	}
	return value, nil
}

func copyOptional(destination, source map[string]any, keys ...string) {
	for _, key := range keys {
		if value, exists := source[key]; exists {
			destination[key] = value
		}
	}
}

func boolValue(value any) bool {
	result, _ := value.(bool)
	return result
}

func intValue(value any, fallback int) int {
	if numeric, ok := value.(float64); ok {
		return int(numeric)
	}
	return fallback
}

func numberValue(value any, fallback float64) float64 {
	if numeric, ok := value.(float64); ok {
		return numeric
	}
	return fallback
}

func durationSeconds(value any) time.Duration {
	if numeric, ok := value.(float64); ok {
		return time.Duration(numeric * float64(time.Second))
	}
	return 0
}
