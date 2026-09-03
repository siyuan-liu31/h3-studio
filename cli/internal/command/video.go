package command

import (
	"context"
	"fmt"
	"time"

	"h3studio/cli/internal/operation"
)

const VideoHelp = `Usage: h3ctl video COMMAND

  h3ctl video compose --spec PATH|- --to PATH [--force] [--timeout 0] [--poll-interval 5s]
  h3ctl video trim SOURCE --start SECONDS --end SECONDS [media trim flags]
  h3ctl video concat PROJECT

compose is the end-to-end long-video command: it pins missing Profile version
metadata, creates the durable project, generates every segment in order, waits,
applies Motion Context head trim inside each chained render, concatenates the
validated 24fps outputs, and atomically downloads the final movie.

Turbo4 is a sampling preset, not a fixed step count. Set each segment's
request.parameters.steps within the selected Profile range. Motion Context uses:
  "continuation": "motion_context",
  "motion_context": {"video_frames": 22, "audio_frames": 24}

Ctrl-C only interrupts the local wait. The returned project_id can be resumed
with h3ctl project get/run/wait/merge/download.

Example:
  h3ctl video compose --spec trilogy.json --to final.mp4 --timeout 0
`

func (r *Runner) runVideo(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, VideoHelp)
		return nil, nil
	}
	switch args[0] {
	case "compose":
		set := newFlags("video compose")
		specPath := set.String("spec", "", "")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *specPath == "" || *to == "" {
			return nil, usage("video compose requires --spec PATH|- --to PATH")
		}
		if *timeout < 0 || *poll < 0 {
			return nil, usage("timeout and poll-interval cannot be negative")
		}
		spec, err := readJSONInput(r.Streams.In, *specPath)
		if err != nil {
			return nil, err
		}
		return r.Service.ComposeVideo(ctx, spec, *to, *force, operation.WaitOptions{
			Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event,
		})
	case "trim":
		return r.runMedia(ctx, append([]string{"trim"}, args[1:]...))
	case "concat":
		return r.runProject(ctx, append([]string{"merge"}, args[1:]...))
	default:
		return nil, usage("unknown video command %q", args[0])
	}
}
