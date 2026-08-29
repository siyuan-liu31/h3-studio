package command

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/resource"
)

const JobHelp = `Usage: h3ctl job COMMAND

  list [--limit 20] [--cursor CURSOR] [--results]
  get JOB
  wait JOB [--timeout 0] [--poll-interval 5s]
  cancel JOB
  download JOB [--index 0] --to PATH [--force]
  save JOB [--index 0] [--name TEXT] [--folder ID]
  workflow JOB [--to workflow.json] [--force]
  delete JOB

JOB may be a bare ID or job:ID#INDEX. Waiting uses short polling requests;
Ctrl-C stops only the local wait and never cancels the remote generation.
Defaults: index=0, limit=20, timeout=0, poll-interval=5s, overwrite disabled.
Example: h3ctl job wait job:ID --timeout 2h --poll-interval 5s
`

func (r *Runner) runJob(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, JobHelp)
		return nil, nil
	}
	action := args[0]
	switch action {
	case "list":
		set := newFlags("job list")
		limit := set.Int("limit", 20, "")
		cursor := set.String("cursor", "", "")
		results := set.Bool("results", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 {
			return nil, usage("job list does not accept positional arguments")
		}
		if *limit < 1 || *limit > 100 {
			return nil, usage("--limit must be 1..100")
		}
		query := url.Values{"limit": {strconv.Itoa(*limit)}}
		if *cursor != "" {
			query.Set("cursor", *cursor)
		}
		if *results {
			query.Set("results", "1")
		}
		return r.Service.API.Get(ctx, "/api/jobs?"+query.Encode())
	case "get":
		if len(args) != 2 {
			return nil, usage("job get requires JOB")
		}
		id, _, err := jobID(args[1])
		if err != nil {
			return nil, err
		}
		return r.Service.API.Get(ctx, "/api/jobs/"+url.PathEscape(id))
	case "wait":
		set := newFlags("job wait")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("job wait requires JOB")
		}
		if *poll <= 0 {
			return nil, usage("--poll-interval must be positive")
		}
		id, _, err := jobID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.Wait(ctx, id, operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
	case "cancel":
		if len(args) != 2 {
			return nil, usage("job cancel requires JOB")
		}
		id, _, err := jobID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/jobs/"+url.PathEscape(id)+"/cancel", nil, &value)
		return value, err
	case "download":
		set := newFlags("job download")
		index := set.Int("index", -1, "")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *to == "" {
			return nil, usage("job download requires JOB and --to PATH")
		}
		id, locatorIndex, err := jobID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		indexProvided := false
		set.Visit(func(item *flag.Flag) {
			if item.Name == "index" {
				indexProvided = true
			}
		})
		if indexProvided && *index < 0 {
			return nil, usage("--index cannot be negative")
		}
		if !indexProvided {
			*index = locatorIndex
		}
		return r.Service.API.Download(ctx, "/api/download?id="+url.QueryEscape(id)+"&index="+strconv.Itoa(*index), *to, *force)
	case "save":
		set := newFlags("job save")
		index := set.Int("index", -1, "")
		name := set.String("name", "", "")
		folder := set.String("folder", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("job save requires JOB")
		}
		id, locatorIndex, err := jobID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		indexProvided := false
		set.Visit(func(item *flag.Flag) {
			if item.Name == "index" {
				indexProvided = true
			}
		})
		if indexProvided && *index < 0 {
			return nil, usage("--index cannot be negative")
		}
		if !indexProvided {
			*index = locatorIndex
		}
		body := map[string]any{"index": *index, "visibility": "library"}
		if *name != "" {
			body["display_name"] = *name
		}
		if *folder != "" {
			if !resource.ValidServerID(*folder) {
				return nil, usage("--folder must be a 32-character lowercase hex ID")
			}
			body["folder_id"] = *folder
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/jobs/"+url.PathEscape(id)+"/assets", body, &value)
		if err == nil {
			_, err = operation.RequireResponseID(value, "asset_id", "id")
		}
		return value, err
	case "workflow":
		set := newFlags("job workflow")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("job workflow requires JOB")
		}
		id, _, err := jobID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		if *to != "" {
			return r.Service.API.Download(ctx, "/api/jobs/"+url.PathEscape(id)+"/workflow?download=1", *to, *force)
		}
		return r.Service.API.Get(ctx, "/api/jobs/"+url.PathEscape(id)+"/workflow")
	case "delete":
		if len(args) != 2 {
			return nil, usage("job delete requires JOB")
		}
		id, _, err := jobID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodDelete, "/api/jobs/"+url.PathEscape(id), nil, &value)
		return value, err
	default:
		return nil, usage("unknown job command %q", action)
	}
}

func jobID(raw string) (string, int, error) {
	if strings.HasPrefix(raw, "job:") {
		locator, err := resource.Parse(raw)
		if err != nil {
			return "", 0, &contract.CLIError{Code: "invalid_locator", Message: err.Error(), Cause: err}
		}
		return locator.ID, locator.Index, nil
	}
	if !resource.ValidServerID(raw) {
		return "", 0, contract.NewError("invalid_locator", "job ID must be 32 lowercase hex characters")
	}
	return raw, 0, nil
}
