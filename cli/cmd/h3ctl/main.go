package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"h3studio/cli/internal/command"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	code := command.Execute(ctx, os.Args[1:], command.IOStreams{In: os.Stdin, Out: os.Stdout, Err: os.Stderr})
	os.Exit(code)
}
