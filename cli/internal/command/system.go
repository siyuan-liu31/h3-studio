package command

import (
	"context"
	"flag"
	"fmt"
	"io"
	"sort"
	"strings"

	"h3studio/cli/internal/config"
	"h3studio/cli/internal/contract"
)

const ContextHelp = `Usage: h3ctl context add|update|list|use|show|test|remove

  context add NAME --server URL [--use]
  context add NAME --ssh-target TARGET [--ssh-port PORT] [--remote-api-port 6020] [--use]
  context update NAME --server URL
  context update NAME [--ssh-target TARGET] [--ssh-port PORT|--clear-ssh-port] [--remote-api-port PORT]
  context use NAME
  context list
  context show [NAME]
  context test [NAME]
  context remove NAME

Direct and SSH connections are mutually exclusive. SSH TARGET is passed to
ssh without a shell and may be a ~/.ssh/config alias or user@host. The remote
API always binds through remote loopback. SSH passwords are never stored;
use keys and ssh-agent. First added context becomes current.

Examples:
  h3ctl context add local --server http://127.0.0.1:6020 --use
  h3ctl context add dev --ssh-target h3-dev --remote-api-port 6020
  h3ctl context update dev --ssh-port 2222
  h3ctl context update dev --clear-ssh-port
  h3ctl context test dev --non-interactive
`

func (r *Runner) runContext(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, ContextHelp)
		return nil, nil
	}
	action := args[0]
	value, err := r.Store.Load()
	if err != nil {
		return nil, err
	}
	switch action {
	case "list":
		if len(args) != 1 {
			return nil, usage("context list does not accept arguments")
		}
		names := make([]string, 0, len(value.Contexts))
		for name := range value.Contexts {
			names = append(names, name)
		}
		sort.Strings(names)
		items := make([]map[string]any, 0, len(names))
		for _, name := range names {
			items = append(items, safeContextView(name, value.Contexts[name], name == value.Current))
		}
		return map[string]any{"contexts": items, "current": value.Current, "config_path": r.Store.Path}, nil
	case "add":
		if len(args) < 2 {
			return nil, usage("context add requires NAME")
		}
		set := newFlags("context add")
		server := set.String("server", "", "direct server URL")
		sshTarget := set.String("ssh-target", "", "SSH alias or user@host")
		sshPort := set.Int("ssh-port", 0, "SSH port (default: ssh config)")
		remotePort := set.Int("remote-api-port", config.DefaultRemoteAPIPort, "remote loopback API port")
		use := set.Bool("use", false, "make current")
		if err := parseFlags(set, args[2:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 {
			return nil, usage("context add accepts exactly one NAME")
		}
		if (*server == "") == (*sshTarget == "") {
			return nil, usage("exactly one of --server or --ssh-target is required")
		}
		if flagWasSet(set, "ssh-port") && (*sshPort < 1 || *sshPort > 65535) {
			return nil, usage("--ssh-port must be between 1 and 65535")
		}
		if flagWasSet(set, "remote-api-port") && (*remotePort < 1 || *remotePort > 65535) {
			return nil, usage("--remote-api-port must be between 1 and 65535")
		}
		candidate := config.Context{Server: *server, SSHTarget: *sshTarget, SSHPort: *sshPort}
		if *sshTarget != "" {
			candidate.RemoteAPIPort = *remotePort
		} else if *sshPort != 0 || flagWasSet(set, "remote-api-port") {
			return nil, usage("--ssh-port and --remote-api-port require --ssh-target")
		}
		normalized, err := config.NormalizeContext(candidate)
		if err != nil {
			return nil, usage("%v", err)
		}
		if !validName(args[1]) {
			return nil, usage("context name is invalid")
		}
		if err := r.Store.Update(func(latest *config.File) error {
			if _, exists := latest.Contexts[args[1]]; exists {
				return &contract.CLIError{Code: "conflict", Message: "context already exists"}
			}
			latest.Contexts[args[1]] = normalized
			if *use || latest.Current == "" {
				latest.Current = args[1]
			}
			value = *latest
			return nil
		}); err != nil {
			return nil, err
		}
		return safeContextView(args[1], normalized, value.Current == args[1]), nil
	case "update":
		if len(args) < 2 {
			return nil, usage("context update requires NAME")
		}
		set := newFlags("context update")
		server := set.String("server", "", "direct server URL")
		sshTarget := set.String("ssh-target", "", "SSH alias or user@host")
		sshPort := set.Int("ssh-port", 0, "SSH port")
		clearSSHPort := set.Bool("clear-ssh-port", false, "use Port from SSH config")
		remotePort := set.Int("remote-api-port", 0, "remote loopback API port")
		if err := parseFlags(set, args[2:]); err != nil {
			return nil, usage("%v", err)
		}
		changed := flagWasSet(set, "server") || flagWasSet(set, "ssh-target") || flagWasSet(set, "ssh-port") || flagWasSet(set, "clear-ssh-port") || flagWasSet(set, "remote-api-port")
		if set.NArg() != 0 || !changed {
			return nil, usage("context update requires at least one connection flag")
		}
		if (flagWasSet(set, "server") && *server == "") || (flagWasSet(set, "ssh-target") && *sshTarget == "") {
			return nil, usage("--server and --ssh-target must not be empty when supplied")
		}
		if flagWasSet(set, "ssh-port") && *clearSSHPort {
			return nil, usage("--ssh-port and --clear-ssh-port are mutually exclusive")
		}
		if flagWasSet(set, "clear-ssh-port") && !*clearSSHPort {
			return nil, usage("--clear-ssh-port=false does not change the context")
		}
		if *server != "" && (*sshTarget != "" || flagWasSet(set, "ssh-port") || *clearSSHPort || flagWasSet(set, "remote-api-port")) {
			return nil, usage("direct and SSH connection flags are mutually exclusive")
		}
		if flagWasSet(set, "ssh-port") && (*sshPort < 1 || *sshPort > 65535) {
			return nil, usage("--ssh-port must be between 1 and 65535")
		}
		if flagWasSet(set, "remote-api-port") && (*remotePort < 1 || *remotePort > 65535) {
			return nil, usage("--remote-api-port must be between 1 and 65535")
		}
		if err := r.Store.Update(func(latest *config.File) error {
			current, ok := latest.Contexts[args[1]]
			if !ok {
				return contract.NewError("not_found", "context not found")
			}
			switch {
			case *server != "":
				current = config.Context{Server: *server}
			case *sshTarget != "":
				if current.SSHTarget == "" {
					current = config.Context{RemoteAPIPort: config.DefaultRemoteAPIPort}
				}
				current.Server, current.SSHTarget = "", *sshTarget
			default:
				if current.SSHTarget == "" {
					return usage("updating a direct context requires --server; use --ssh-target to switch modes")
				}
			}
			if flagWasSet(set, "ssh-port") {
				current.SSHPort = *sshPort
			}
			if *clearSSHPort {
				current.SSHPort = 0
			}
			if flagWasSet(set, "remote-api-port") {
				current.RemoteAPIPort = *remotePort
			}
			normalized, err := config.NormalizeContext(current)
			if err != nil {
				return usage("%v", err)
			}
			latest.Contexts[args[1]] = normalized
			value, current = *latest, normalized
			return nil
		}); err != nil {
			return nil, err
		}
		return safeContextView(args[1], value.Contexts[args[1]], value.Current == args[1]), nil
	case "use":
		if len(args) != 2 {
			return nil, usage("context use requires NAME")
		}
		if err := r.Store.Update(func(latest *config.File) error {
			if _, ok := latest.Contexts[args[1]]; !ok {
				return contract.NewError("not_found", "context not found")
			}
			latest.Current = args[1]
			value = *latest
			return nil
		}); err != nil {
			return nil, err
		}
		return map[string]any{"current": args[1]}, nil
	case "show":
		if len(args) > 2 {
			return nil, usage("context show accepts at most NAME")
		}
		name := value.Current
		if len(args) > 1 {
			name = args[1]
		}
		item, ok := value.Contexts[name]
		if !ok {
			return nil, contract.NewError("not_found", "context not found")
		}
		return safeContextView(name, item, name == value.Current), nil
	case "test":
		if len(args) > 2 {
			return nil, usage("context test accepts at most NAME")
		}
		name := value.Current
		if len(args) > 1 {
			name = args[1]
		}
		item, ok := value.Contexts[name]
		if !ok {
			return nil, contract.NewError("not_found", "context not found")
		}
		if err := r.connectResolved(ctx, name, item); err != nil {
			return nil, err
		}
		health, err := r.Service.Health(ctx)
		cleanupErr := r.closeConnection()
		if err = mergeCleanupError(err, cleanupErr); err != nil {
			return nil, err
		}
		result := safeContextView(name, item, name == value.Current)
		result["health"] = health
		return result, nil
	case "remove":
		if len(args) != 2 {
			return nil, usage("context remove requires NAME")
		}
		if err := r.Store.Update(func(latest *config.File) error {
			if _, ok := latest.Contexts[args[1]]; !ok {
				return contract.NewError("not_found", "context not found")
			}
			delete(latest.Contexts, args[1])
			if latest.Current == args[1] {
				latest.Current = ""
			}
			value = *latest
			return nil
		}); err != nil {
			return nil, err
		}
		return map[string]any{"name": args[1], "removed": true}, nil
	default:
		return nil, usage("unknown context command %q", action)
	}
}

func safeContextView(name string, item config.Context, current bool) map[string]any {
	normalized, err := config.NormalizeContext(item)
	if err != nil {
		return map[string]any{"name": name, "current": current, "status": "invalid", "error": "stored context has invalid connection settings"}
	}
	result := map[string]any{"name": name, "current": current, "status": "valid"}
	if normalized.Server != "" {
		result["mode"], result["server"] = "direct", normalized.Server
	} else {
		result["mode"], result["ssh_target"] = "ssh", normalized.SSHTarget
		result["remote_api_port"] = normalized.RemoteAPIPort
		if normalized.SSHPort != 0 {
			result["ssh_port"] = normalized.SSHPort
		}
	}
	return result
}

func (r *Runner) runDoctor(ctx context.Context, args []string) (any, error) {
	if explicitHelp(args) {
		fmt.Fprint(r.Streams.Out, "Usage: h3ctl doctor\nChecks /health and /api/capabilities. No command flags.\nExample: h3ctl --context dev doctor --json\n")
		return nil, nil
	}
	if len(args) > 0 {
		return nil, usage("doctor does not accept arguments")
	}
	health, err := r.Service.Health(ctx)
	if err != nil {
		return nil, err
	}
	caps, capErr := r.Service.Capabilities(ctx)
	result := map[string]any{"context": r.Service.Context, "server": r.Service.API.BaseURL, "health": health}
	if capErr != nil {
		result["capabilities_error"] = capErr.Error()
		return result, capErr
	}
	result["capabilities"] = caps
	return result, nil
}

func (r *Runner) runCapability(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, "Usage: h3ctl capability list|show [video|image]\nNo command flags. Example: h3ctl capability show video --json\n")
		return nil, nil
	}
	action := args[0]
	switch action {
	case "list":
		if len(args) != 1 {
			return nil, usage("capability list does not accept arguments")
		}
	case "show":
		if len(args) != 2 {
			return nil, usage("capability show requires video or image")
		}
		if args[1] != "video" && args[1] != "image" {
			return nil, usage("capability show requires video or image")
		}
	default:
		return nil, usage("unknown capability command %q", action)
	}
	value, err := r.Service.Capabilities(ctx)
	if err != nil {
		return nil, err
	}
	if action == "list" {
		return value, nil
	}
	if action == "show" {
		selected, ok := value[args[1]]
		if !ok {
			return nil, contract.NewError("not_found", "capability not found")
		}
		return selected, nil
	}
	return nil, usage("unknown capability command %q", action)
}

func (r *Runner) runProfile(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, "Usage: h3ctl profile list|show [PROFILE_ID]\nProfiles include immutable version and manifest digest. No command flags.\nExample: h3ctl profile show minimax-h3-fl2va --json\n")
		return nil, nil
	}
	action := args[0]
	switch action {
	case "list":
		if len(args) != 1 {
			return nil, usage("profile list does not accept arguments")
		}
	case "show":
		if len(args) != 2 {
			return nil, usage("profile show requires PROFILE_ID")
		}
	default:
		return nil, usage("unknown profile command %q", action)
	}
	caps, err := r.Service.Capabilities(ctx)
	if err != nil {
		return nil, err
	}
	profiles, _ := caps["profiles"].([]any)
	switch action {
	case "list":
		return map[string]any{"profiles": profiles}, nil
	case "show":
		for _, raw := range profiles {
			item, _ := raw.(map[string]any)
			if item["id"] == args[1] {
				return item, nil
			}
		}
		return nil, contract.NewError("not_found", "profile not found")
	}
	return nil, usage("unknown profile command %q", action)
}

func help(args []string) bool {
	if len(args) == 0 {
		return true
	}
	return explicitHelp(args)
}

func explicitHelp(args []string) bool {
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			return false
		}
		if index == 0 && arg == "help" {
			return true
		}
		if arg == "--help" || arg == "-h" || arg == "help" {
			if arg != "help" {
				return true
			}
		}
		if strings.HasPrefix(arg, "-") && commandFlagNeedsValue("", "", arg) && !strings.Contains(arg, "=") {
			index++
		}
	}
	return false
}
func newFlags(name string) *flag.FlagSet {
	set := flag.NewFlagSet(name, flag.ContinueOnError)
	set.SetOutput(io.Discard)
	return set
}

// parseFlags accepts flags before or after positional arguments while retaining
// flag.FlagSet's strict value parsing. Singleton flags may not be repeated;
// callers must explicitly list repeatable collection flags.
func parseFlags(set *flag.FlagSet, args []string, repeatable ...string) error {
	repeat := map[string]bool{}
	for _, name := range repeatable {
		repeat[name] = true
	}
	seen := map[string]bool{}
	flags, positional := []string{}, []string{}
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			positional = append(positional, args[index+1:]...)
			break
		}
		if !strings.HasPrefix(arg, "-") || arg == "-" {
			positional = append(positional, arg)
			continue
		}
		nameValue := strings.TrimLeft(arg, "-")
		name, _, hasEquals := strings.Cut(nameValue, "=")
		item := set.Lookup(name)
		if item == nil {
			return usage("unknown flag --%s", name)
		}
		if seen[name] && !repeat[name] {
			return usage("flag --%s may only be supplied once", name)
		}
		seen[name] = true
		flags = append(flags, arg)
		boolean := false
		if getter, ok := item.Value.(interface{ IsBoolFlag() bool }); ok {
			boolean = getter.IsBoolFlag()
		}
		if !hasEquals && !boolean {
			if index+1 >= len(args) {
				return usage("flag --%s requires a value", name)
			}
			index++
			flags = append(flags, args[index])
		}
	}
	return set.Parse(append(flags, positional...))
}
func validName(value string) bool {
	if value == "" {
		return false
	}
	for _, c := range value {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("._-", c)) {
			return false
		}
	}
	return true
}

func flagWasSet(set *flag.FlagSet, name string) bool {
	found := false
	set.Visit(func(item *flag.Flag) {
		if item.Name == name {
			found = true
		}
	})
	return found
}
