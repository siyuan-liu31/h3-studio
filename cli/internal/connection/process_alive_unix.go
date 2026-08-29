//go:build !windows

package connection

import (
	"os"
	"syscall"
)

func processExited(process *os.Process) bool {
	if process == nil {
		return true
	}
	return process.Signal(syscall.Signal(0)) != nil
}
