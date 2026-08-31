package resource

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Kind string

const (
	Local  Kind = "file"
	Asset  Kind = "asset"
	Job    Kind = "job"
	Media  Kind = "media"
	Remote Kind = "remote"
)

type Locator struct {
	Raw     string `json:"raw"`
	Kind    Kind   `json:"kind"`
	Path    string `json:"path,omitempty"`
	ID      string `json:"id,omitempty"`
	Index   int    `json:"index,omitempty"`
	Context string `json:"context,omitempty"`
}

func Parse(raw string) (Locator, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Locator{}, fmt.Errorf("locator cannot be empty")
	}
	if strings.HasPrefix(raw, "asset:") {
		return idLocator(raw, "asset:", Asset)
	}
	if strings.HasPrefix(raw, "media:") {
		return idLocator(raw, "media:", Media)
	}
	if strings.HasPrefix(raw, "job:") {
		value := strings.TrimPrefix(raw, "job:")
		index := 0
		if before, after, ok := strings.Cut(value, "#"); ok {
			value = before
			parsed, err := strconv.Atoi(after)
			if err != nil || parsed < 0 {
				return Locator{}, fmt.Errorf("job output index must be a non-negative integer")
			}
			index = parsed
		}
		if !ValidServerID(value) {
			return Locator{}, fmt.Errorf("invalid job id")
		}
		return Locator{Raw: raw, Kind: Job, ID: value, Index: index}, nil
	}
	if strings.HasPrefix(raw, "h3://") {
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return Locator{}, fmt.Errorf("invalid h3 locator")
		}
		parts := strings.Split(strings.Trim(u.Path, "/"), "/")
		if len(parts) != 2 || parts[0] != "assets" || !ValidServerID(parts[1]) {
			return Locator{}, fmt.Errorf("remote locator must be h3://CONTEXT/assets/ID")
		}
		return Locator{Raw: raw, Kind: Remote, Context: u.Host, ID: parts[1]}, nil
	}
	path := raw
	if strings.HasPrefix(raw, "file:") {
		u, err := url.Parse(raw)
		if err != nil || u.Path == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Host != "" && !strings.EqualFold(u.Host, "localhost")) {
			return Locator{}, fmt.Errorf("invalid file URI")
		}
		path = u.Path
	} else if strings.Contains(raw, "://") {
		return Locator{}, fmt.Errorf("unsupported locator scheme")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return Locator{}, err
	}
	info, err := os.Stat(abs)
	if err != nil {
		return Locator{}, err
	}
	if info.IsDir() {
		return Locator{}, fmt.Errorf("expected a file, got directory")
	}
	return Locator{Raw: raw, Kind: Local, Path: abs}, nil
}

func idLocator(raw, prefix string, kind Kind) (Locator, error) {
	id := strings.TrimPrefix(raw, prefix)
	if !ValidServerID(id) {
		return Locator{}, fmt.Errorf("invalid %s id", kind)
	}
	return Locator{Raw: raw, Kind: kind, ID: id}, nil
}

// ValidServerID is the single public identifier contract used by MiniMax H3 Video Studio.
// Server-created assets, jobs, derivations and projects are lowercase UUID
// hex strings without separators.
func ValidServerID(value string) bool {
	if len(value) != 32 {
		return false
	}
	for _, char := range value {
		if !(char >= '0' && char <= '9' || char >= 'a' && char <= 'f') {
			return false
		}
	}
	return true
}

func (l Locator) DeriveSource() map[string]any {
	switch l.Kind {
	case Asset, Remote:
		return map[string]any{"type": "asset", "asset_id": l.ID}
	case Job:
		return map[string]any{"type": "job", "job_id": l.ID, "index": l.Index}
	case Media:
		return map[string]any{"type": "derivation", "receipt_id": l.ID}
	default:
		return nil
	}
}
