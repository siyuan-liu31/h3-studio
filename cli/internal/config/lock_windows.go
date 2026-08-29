//go:build windows

package config

import (
	"os"
	"syscall"
	"unsafe"
)

const lockfileExclusiveLock = 0x00000002

var (
	kernel32     = syscall.NewLazyDLL("kernel32.dll")
	lockFileEx   = kernel32.NewProc("LockFileEx")
	unlockFileEx = kernel32.NewProc("UnlockFileEx")
)

type fileLock struct {
	file       *os.File
	overlapped syscall.Overlapped
}

func acquireFileLock(path string) (*fileLock, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	lock := &fileLock{file: file}
	result, _, callErr := lockFileEx.Call(
		file.Fd(), lockfileExclusiveLock, 0, 1, 0,
		uintptr(unsafe.Pointer(&lock.overlapped)),
	)
	if result == 0 {
		file.Close()
		return nil, callErr
	}
	return lock, nil
}

func (lock *fileLock) Close() error {
	result, _, callErr := unlockFileEx.Call(
		lock.file.Fd(), 0, 1, 0,
		uintptr(unsafe.Pointer(&lock.overlapped)),
	)
	closeErr := lock.file.Close()
	if result == 0 {
		return callErr
	}
	return closeErr
}
