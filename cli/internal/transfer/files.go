package transfer

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

func Collect(path string, recursive bool, includes []string) ([]string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	stat, err := os.Lstat(abs)
	if err != nil {
		return nil, err
	}
	if stat.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("symbolic links are not accepted: %s", abs)
	}
	if !stat.IsDir() {
		return []string{abs}, nil
	}
	if !recursive {
		return nil, fmt.Errorf("directory upload requires --recursive")
	}
	var files []string
	err = filepath.WalkDir(abs, func(current string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("symbolic links are not accepted: %s", current)
		}
		if entry.IsDir() {
			return nil
		}
		if len(includes) > 0 {
			matched := false
			for _, pattern := range includes {
				ok, patternErr := filepath.Match(pattern, entry.Name())
				if patternErr != nil {
					return fmt.Errorf("invalid include pattern %q: %w", pattern, patternErr)
				}
				matched = matched || ok
			}
			if !matched {
				return nil
			}
		}
		files = append(files, current)
		return nil
	})
	sort.Strings(files)
	return files, err
}
