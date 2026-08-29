//go:build windows

package connection

import "os"

func processExited(process *os.Process) bool {
	return process == nil
}
