package command

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/config"
	"h3studio/cli/internal/connection"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/resource"
	"h3studio/cli/internal/transfer"
)

type stringsFlag []string

func (s *stringsFlag) String() string         { return strings.Join(*s, ",") }
func (s *stringsFlag) Set(value string) error { *s = append(*s, value); return nil }

const AssetHelp = `Usage: h3ctl asset COMMAND

Commands:
  upload PATH [--kind auto|image|video|audio] [--recursive] [--include GLOB]
  download ASSET [--to PATH] [--force]
  copy h3://SOURCE/assets/ID --to-context NAME
  list [--query TEXT] [--folder ID]
  get ASSET
  update ASSET [--name TEXT] [--folder ID|root]
  pin ASSET [--off]
  delete ASSET

Directory uploads require --recursive. --include is repeatable and matched
against each base filename. Results are sorted for deterministic Agent runs.
Downloads write a unique same-directory .part and atomically commit on completion.

Defaults: --kind auto; overwrite disabled; recursive disabled.
Example: h3ctl asset upload ./media --recursive --include '*.mp4'
`

func (r *Runner) runAsset(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, AssetHelp)
		return nil, nil
	}
	action := args[0]
	switch action {
	case "upload":
		set := newFlags("asset upload")
		kind := set.String("kind", "auto", "")
		recursive := set.Bool("recursive", false, "")
		var includes stringsFlag
		set.Var(&includes, "include", "")
		if err := parseFlags(set, args[1:], "include"); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("asset upload requires PATH")
		}
		if *kind != "auto" && *kind != "image" && *kind != "video" && *kind != "audio" {
			return nil, usage("--kind must be auto, image, video, or audio")
		}
		files, err := transfer.Collect(set.Arg(0), *recursive, includes)
		if err != nil {
			return nil, &contract.CLIError{Code: "invalid_argument", Message: err.Error(), Cause: err}
		}
		if len(files) == 0 {
			return nil, contract.NewError("not_found", "no files matched")
		}
		results := make([]any, 0, len(files))
		for _, file := range files {
			value, uploadErr := r.Service.Upload(ctx, file, *kind)
			if uploadErr != nil {
				return nil, &contract.CLIError{Code: "partial_failure", Message: uploadErr.Error(), Details: map[string]any{"completed": results, "failed_path": file}, Cause: uploadErr}
			}
			results = append(results, map[string]any{"path": file, "result": value})
		}
		return map[string]any{"uploads": results, "count": len(results)}, nil
	case "list":
		set := newFlags("asset list")
		query := set.String("query", "", "")
		folder := set.String("folder", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 {
			return nil, usage("asset list does not accept positional arguments")
		}
		values := url.Values{}
		if *query != "" {
			values.Set("q", *query)
		}
		if *folder != "" {
			if !resource.ValidServerID(*folder) {
				return nil, usage("--folder must be a 32-character lowercase hex ID")
			}
			values.Set("folder_id", *folder)
		}
		return r.Service.API.Get(ctx, api.Query("/api/assets", values))
	case "get":
		if len(args) != 2 {
			return nil, usage("asset get requires ASSET")
		}
		id, err := assetID(args[1])
		if err != nil {
			return nil, err
		}
		return r.Service.API.Get(ctx, "/api/assets/"+url.PathEscape(id))
	case "download":
		set := newFlags("asset download")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("asset download requires ASSET")
		}
		id, err := assetID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		if *to == "" {
			meta, metaErr := r.Service.API.Get(ctx, "/api/assets/"+url.PathEscape(id))
			if metaErr != nil {
				return nil, metaErr
			}
			*to = safeDownloadName(stringAny(meta["filename"], stringAny(meta["display_name"], id)), id)
		}
		return r.Service.API.Download(ctx, "/api/assets/"+url.PathEscape(id)+"/content", *to, *force)
	case "update":
		set := newFlags("asset update")
		name := set.String("name", "", "")
		folder := set.String("folder", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("asset update requires ASSET")
		}
		body := map[string]any{}
		if *name != "" {
			body["display_name"] = *name
		}
		if *folder != "" {
			if *folder == "root" {
				body["folder_id"] = nil
			} else {
				if !resource.ValidServerID(*folder) {
					return nil, usage("--folder must be root or a 32-character lowercase hex ID")
				}
				body["folder_id"] = *folder
			}
		}
		if len(body) == 0 {
			return nil, usage("provide --name and/or --folder")
		}
		id, err := assetID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPatch, "/api/assets/"+url.PathEscape(id), body, &value)
		return value, err
	case "pin":
		set := newFlags("asset pin")
		off := set.Bool("off", false, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 {
			return nil, usage("asset pin requires ASSET")
		}
		id, err := assetID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPatch, "/api/assets/"+url.PathEscape(id), map[string]any{"pinned": !*off}, &value)
		return value, err
	case "delete":
		if len(args) != 2 {
			return nil, usage("asset delete requires ASSET")
		}
		id, err := assetID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodDelete, "/api/assets/"+url.PathEscape(id), nil, &value)
		return value, err
	case "copy":
		return r.copyAsset(ctx, args[1:])
	default:
		return nil, usage("unknown asset command %q", action)
	}
}

func (r *Runner) copyAsset(ctx context.Context, args []string) (any, error) {
	set := newFlags("asset copy")
	destination := set.String("to-context", "", "")
	if err := parseFlags(set, args); err != nil {
		return nil, usage("%v", err)
	}
	if set.NArg() != 1 || *destination == "" {
		return nil, usage("asset copy requires h3://SOURCE/assets/ID and --to-context NAME")
	}
	return r.copyAssetValues(ctx, set.Arg(0), *destination)
}

func (r *Runner) copyAssetValues(ctx context.Context, sourceValue, destination string) (result map[string]any, returnErr error) {
	locator, err := resource.Parse(sourceValue)
	if err != nil || locator.Kind != resource.Remote {
		return nil, usage("copy source must be h3://CONTEXT/assets/ID")
	}
	_, sourceConfig, err := config.Resolve(r.Store, locator.Context, "")
	if err != nil {
		return nil, err
	}
	_, destConfig, err := config.Resolve(r.Store, destination, "")
	if err != nil {
		return nil, err
	}
	options := r.ConnectionOptions
	options.NonInteractive, options.Stderr = r.Globals.NonInteractive, r.Streams.Err
	sourceSession, err := connection.Open(ctx, sourceConfig, options)
	if err != nil {
		return nil, err
	}
	defer func() {
		returnErr = mergeCleanupError(returnErr, sourceSession.Close())
		if returnErr != nil {
			result = nil
		}
	}()
	destSession, err := connection.Open(ctx, destConfig, options)
	if err != nil {
		return nil, err
	}
	defer func() {
		returnErr = mergeCleanupError(returnErr, destSession.Close())
		if returnErr != nil {
			result = nil
		}
	}()
	source := api.NewWithTimeouts(sourceSession.BaseURL, r.Globals.ControlTimeout, r.Globals.TransferTimeout, r.Globals.MediaTimeout)
	dest := api.NewWithTimeouts(destSession.BaseURL, r.Globals.ControlTimeout, r.Globals.TransferTimeout, r.Globals.MediaTimeout)
	meta, err := source.Get(ctx, "/api/assets/"+url.PathEscape(locator.ID))
	if err != nil {
		return nil, err
	}
	name := safeDownloadName(stringAny(meta["filename"], locator.ID), locator.ID)
	tempDir, err := os.MkdirTemp("", "h3ctl-copy-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(tempDir)
	temp := filepath.Join(tempDir, name)
	if _, err = source.Download(ctx, "/api/assets/"+url.PathEscape(locator.ID)+"/content", temp, false); err != nil {
		return nil, err
	}
	uploaded, err := dest.Upload(ctx, temp, stringAny(meta["kind"], "auto"))
	if err != nil {
		return nil, err
	}
	if _, err := operation.RequireResponseID(uploaded, "asset_id", "id"); err != nil {
		return nil, err
	}
	return map[string]any{"source": sourceValue, "destination_context": destination, "asset": uploaded}, nil
}

func assetID(raw string) (string, error) {
	if strings.HasPrefix(raw, "asset:") {
		locator, err := resource.Parse(raw)
		if err != nil {
			return "", &contract.CLIError{Code: "invalid_locator", Message: err.Error(), Cause: err}
		}
		return locator.ID, nil
	}
	if !resource.ValidServerID(raw) {
		return "", contract.NewError("invalid_locator", "asset ID must be 32 lowercase hex characters")
	}
	return raw, nil
}
func stringAny(value any, fallback string) string {
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}
func safeDownloadName(value, fallback string) string {
	name := path.Base(strings.ReplaceAll(value, "\\", "/"))
	invalid := name == "" || name == "." || name == ".." || name == "/" || strings.ContainsAny(value, "/\\") || strings.TrimRight(name, ". ") != name
	for _, char := range name {
		if char < 32 || strings.ContainsRune(`<>:"/\|?*`, char) {
			invalid = true
		}
	}
	base := strings.ToUpper(strings.SplitN(name, ".", 2)[0])
	if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" || len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && base[3] >= '1' && base[3] <= '9' {
		invalid = true
	}
	if invalid {
		if fallback == value {
			return "download.bin"
		}
		return safeDownloadName(fallback, fallback)
	}
	return name
}
