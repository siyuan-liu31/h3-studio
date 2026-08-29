package command

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/resource"
)

const ProjectHelp = `Usage: h3ctl project COMMAND

  create --spec PATH|-
  apply PROJECT --spec PATH|-
  list | get PROJECT | delete PROJECT
  run PROJECT [--segment ID ...]
  wait PROJECT [--timeout 0] [--poll-interval 5s]
  stop PROJECT
  rerun PROJECT --segment ID
  merge PROJECT
  download PROJECT --to PATH [--force]

Project specs are the same JSON objects accepted by /api/video-projects.
Defaults: timeout=0, poll-interval=5s, overwrite disabled.
Example: h3ctl project run PROJECT --segment SEGMENT
`

func (r *Runner) runProject(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, ProjectHelp)
		return nil, nil
	}
	action := args[0]
	switch action {
	case "create":
		set := newFlags("project create")
		spec := set.String("spec", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *spec == "" {
			return nil, usage("project create requires --spec")
		}
		body, err := readJSONInput(r.Streams.In, *spec)
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/video-projects", body, &value)
		if err == nil {
			_, err = operation.RequireResponseID(value, "project_id", "id")
		}
		return value, err
	case "apply":
		set := newFlags("project apply")
		spec := set.String("spec", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *spec == "" {
			return nil, usage("project apply requires PROJECT --spec PATH|-")
		}
		body, err := readJSONInput(r.Streams.In, *spec)
		if err != nil {
			return nil, err
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPut, "/api/video-projects/"+url.PathEscape(id), body, &value)
		if err == nil {
			_, err = operation.RequireResponseID(value, "project_id", "id")
		}
		return value, err
	case "list":
		if len(args) != 1 {
			return nil, usage("project list does not accept arguments")
		}
		return r.Service.API.Get(ctx, "/api/video-projects")
	case "get":
		if len(args) != 2 {
			return nil, usage("project get requires PROJECT")
		}
		id, err := projectID(args[1])
		if err != nil {
			return nil, err
		}
		return r.Service.API.Get(ctx, "/api/video-projects/"+url.PathEscape(id))
	case "run":
		set := newFlags("project run")
		var segments stringsFlag
		set.Var(&segments, "segment", "")
		if err := parseFlags(set, args[1:], "segment"); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("project run requires PROJECT")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		for _, segment := range segments {
			if !resource.ValidServerID(segment) {
				return nil, usage("segment ID must be 32 lowercase hex characters")
			}
		}
		body := map[string]any{}
		if len(segments) > 0 {
			body["segment_ids"] = []string(segments)
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/video-projects/"+url.PathEscape(id)+"/run", body, &value)
		return value, err
	case "wait":
		set := newFlags("project wait")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("project wait requires PROJECT")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.WaitProject(ctx, id, *timeout, *poll, r.Printer.Event)
	case "stop", "merge":
		if len(args) != 2 {
			return nil, usage("project %s requires PROJECT", action)
		}
		id, err := projectID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/video-projects/"+url.PathEscape(id)+"/"+action, map[string]any{}, &value)
		return value, err
	case "rerun":
		set := newFlags("project rerun")
		segment := set.String("segment", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *segment == "" {
			return nil, usage("project rerun requires PROJECT --segment ID")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		if !resource.ValidServerID(*segment) {
			return nil, usage("segment ID must be 32 lowercase hex characters")
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/video-projects/"+url.PathEscape(id)+"/segments/"+url.PathEscape(*segment)+"/run", map[string]any{}, &value)
		return value, err
	case "download":
		set := newFlags("project download")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *to == "" {
			return nil, usage("project download requires PROJECT --to PATH")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.API.Download(ctx, "/api/video-projects/"+url.PathEscape(id)+"/merged/download", *to, *force)
	case "delete":
		if len(args) != 2 {
			return nil, usage("project delete requires PROJECT")
		}
		id, err := projectID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodDelete, "/api/video-projects/"+url.PathEscape(id), nil, &value)
		return value, err
	default:
		return nil, usage("unknown project command %q", action)
	}
}

func projectID(raw string) (string, error) {
	if !resource.ValidServerID(raw) {
		return "", contract.NewError("invalid_locator", "project ID must be 32 lowercase hex characters")
	}
	return raw, nil
}

func readJSONInput(in io.Reader, path string) (map[string]any, error) {
	raw, err := readInput(in, path)
	if err != nil {
		return nil, err
	}
	value := map[string]any{}
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, &contract.CLIError{Code: "invalid_spec", Message: err.Error(), Cause: err}
	}
	return value, nil
}
