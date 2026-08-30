package command

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strings"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/resource"
)

const MediaHelp = `Usage: h3ctl media COMMAND

  frame SOURCE --position first|last|current [--at SECONDS]
  endpoints SOURCE
  trim SOURCE --start SECONDS --end SECONDS [--audio]
  extract-audio SOURCE
  remove-audio SOURCE
  prepare-reference SOURCE [--preset h3-low-token | controlled sizing flags]
  list
  get MEDIA
  download MEDIA --to PATH [--force]
  save MEDIA [--name TEXT] [--folder ID]
  delete MEDIA

SOURCE accepts local video, asset:ID, job:ID#INDEX, or media:ID.
Frame, trim, extract-audio, and remove-audio also accept --name TEXT.
Derivations remain independent receipts until explicitly saved as an asset.
Defaults: frame position=current; overwrite disabled.
Example: h3ctl media frame job:ID#0 --position last
         h3ctl media prepare-reference asset:ID --preset h3-low-token
`

func (r *Runner) runMedia(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, MediaHelp)
		return nil, nil
	}
	action := args[0]
	switch action {
	case "prepare-reference":
		set := newFlags("media prepare-reference")
		preset := set.String("preset", "", "")
		shortEdge := set.Int("max-short-edge", 480, "")
		longEdge := set.Int("max-long-edge", 864, "")
		fps := set.Int("fps", 24, "")
		maxDuration := set.Float64("max-duration", 15, "")
		audioMode := set.String("audio", "", "")
		fit := set.String("fit", "contain", "")
		alignment := set.Int("alignment", 32, "")
		padMode := set.String("pad-mode", "edge", "")
		name := set.String("name", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("media prepare-reference requires SOURCE")
		}
		if *preset != "" && *preset != "h3-low-token" {
			return nil, usage("--preset must be h3-low-token")
		}
		if *audioMode == "" && *preset == "" {
			return nil, usage("--audio keep|remove is required unless --preset h3-low-token is used")
		}
		if *audioMode != "" && *audioMode != "keep" && *audioMode != "remove" {
			return nil, usage("--audio must be keep or remove")
		}
		if *shortEdge <= 0 || *longEdge <= 0 || *shortEdge > *longEdge || *fps != 24 || *maxDuration <= 0 || math.IsNaN(*maxDuration) || math.IsInf(*maxDuration, 0) || *fit != "contain" || *alignment != 32 || *padMode != "edge" {
			return nil, usage("prepare-reference requires positive aligned H3 sizing, 24 fps, contain fit, and edge padding")
		}
		body := map[string]any{
			"operation": "prepare_h3_reference", "max_short_edge": *shortEdge,
			"max_long_edge": *longEdge, "fps": *fps, "max_duration": *maxDuration,
			"fit": *fit, "alignment": *alignment, "pad_mode": *padMode,
		}
		if *preset != "" {
			body["preset"] = *preset
		}
		if *audioMode != "" {
			body["audio"] = *audioMode
		}
		if *name != "" {
			body["display_name"] = *name
		}
		value, err := r.Service.DeriveWithEvents(ctx, set.Arg(0), body, r.Printer.Event)
		if err == nil {
			if id, ok := value["id"].(string); ok {
				value["locator"] = "media:" + id
			}
		}
		return value, err
	case "frame":
		set := newFlags("media frame")
		position := set.String("position", "current", "")
		at := set.Float64("at", -1, "")
		name := set.String("name", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("media frame requires SOURCE")
		}
		if *position != "first" && *position != "last" && *position != "current" {
			return nil, usage("--position must be first, last, or current")
		}
		body := map[string]any{"operation": "frame", "position": *position}
		if *position == "current" {
			if *at < 0 || math.IsNaN(*at) || math.IsInf(*at, 0) {
				return nil, usage("current frame requires --at SECONDS")
			}
			body["time"] = *at
		} else if *at >= 0 {
			return nil, usage("--at is only valid with --position current")
		}
		if *name != "" {
			body["display_name"] = *name
		}
		return r.Service.Derive(ctx, set.Arg(0), body)
	case "endpoints":
		set := newFlags("media endpoints")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("media endpoints requires SOURCE")
		}
		first, err := r.Service.Derive(ctx, set.Arg(0), map[string]any{"operation": "frame", "position": "first"})
		if err != nil {
			return nil, err
		}
		last, err := r.Service.Derive(ctx, set.Arg(0), map[string]any{"operation": "frame", "position": "last"})
		if err != nil {
			return nil, &contract.CLIError{Code: "partial_failure", Message: err.Error(), Details: map[string]any{"first": first}, Cause: err}
		}
		return map[string]any{"first": first, "last": last}, nil
	case "trim":
		set := newFlags("media trim")
		start := set.Float64("start", -1, "")
		end := set.Float64("end", -1, "")
		audio := set.Bool("audio", false, "")
		name := set.String("name", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *start < 0 || *end <= *start || math.IsNaN(*start) || math.IsNaN(*end) || math.IsInf(*start, 0) || math.IsInf(*end, 0) {
			return nil, usage("media trim requires SOURCE and valid --start/--end")
		}
		operation := "video_trim"
		if *audio {
			operation = "audio_trim"
		}
		body := map[string]any{"operation": operation, "start": *start, "end": *end}
		if *name != "" {
			body["display_name"] = *name
		}
		return r.Service.Derive(ctx, set.Arg(0), body)
	case "extract-audio", "remove-audio":
		set := newFlags("media " + action)
		name := set.String("name", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("media %s requires SOURCE", action)
		}
		operation := strings.ReplaceAll(action, "-", "_")
		body := map[string]any{"operation": operation}
		if *name != "" {
			body["display_name"] = *name
		}
		return r.Service.Derive(ctx, set.Arg(0), body)
	case "list":
		if len(args) != 1 {
			return nil, usage("media list does not accept arguments")
		}
		return r.Service.API.Get(ctx, "/api/derivations")
	case "get":
		if len(args) != 2 {
			return nil, usage("media get requires MEDIA")
		}
		id, err := mediaID(args[1])
		if err != nil {
			return nil, err
		}
		return r.Service.API.Get(ctx, "/api/derivations/"+url.PathEscape(id))
	case "download":
		set := newFlags("media download")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *to == "" {
			return nil, usage("media download requires MEDIA and --to PATH")
		}
		id, err := mediaID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.API.Download(ctx, "/api/derivations/"+url.PathEscape(id)+"/download", *to, *force)
	case "save":
		set := newFlags("media save")
		name := set.String("name", "", "")
		folder := set.String("folder", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("media save requires MEDIA")
		}
		id, err := mediaID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		body := map[string]any{}
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
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/derivations/"+url.PathEscape(id)+"/assets", body, &value)
		if err == nil {
			_, err = operation.RequireResponseID(value, "asset_id", "id")
		}
		return value, err
	case "delete":
		if len(args) != 2 {
			return nil, usage("media delete requires MEDIA")
		}
		id, err := mediaID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodDelete, "/api/derivations/"+url.PathEscape(id), nil, &value)
		return value, err
	default:
		return nil, usage("unknown media command %q", action)
	}
}

func mediaID(raw string) (string, error) {
	if strings.HasPrefix(raw, "media:") {
		locator, err := resource.Parse(raw)
		if err != nil {
			return "", &contract.CLIError{Code: "invalid_locator", Message: err.Error(), Cause: err}
		}
		return locator.ID, nil
	}
	if !resource.ValidServerID(raw) {
		return "", contract.NewError("invalid_locator", "media ID must be 32 lowercase hex characters")
	}
	return raw, nil
}
