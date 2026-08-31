package command

import (
	"context"
	"fmt"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
)

const OperationHelp = `Usage: h3ctl operation list|schema|run

  operation list
  operation schema NAME
  operation run NAME --input PATH|-

This is the stable Agent entry point. Inputs and outputs are JSON. The registry,
recursive schemas, validation, and execution live in internal/operation so a
future workflow runner uses exactly the same atomic implementations.
Defaults are declared by each operation schema.
Example: h3ctl operation run media.frame --input request.json --json
`

type operationRunInvocation struct {
	Name      string
	InputPath string
}

func parseOperationRunArgs(args []string) (operationRunInvocation, error) {
	set := newFlags("operation run")
	inputPath := set.String("input", "", "")
	if err := parseFlags(set, args); err != nil {
		return operationRunInvocation{}, usage("%v", err)
	}
	if set.NArg() != 1 || *inputPath == "" {
		return operationRunInvocation{}, usage("operation run requires NAME --input PATH|-")
	}
	return operationRunInvocation{Name: set.Arg(0), InputPath: *inputPath}, nil
}

func (r *Runner) runOperation(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, OperationHelp)
		return nil, nil
	}
	switch args[0] {
	case "list":
		if len(args) != 1 {
			return nil, usage("operation list does not accept arguments")
		}
		definitions := operation.Definitions()
		names := make([]string, 0, len(definitions))
		for _, definition := range definitions {
			names = append(names, definition.Name)
		}
		return map[string]any{"operations": names, "schema_version": operation.OperationSchemaVersion}, nil
	case "schema":
		if len(args) != 2 {
			return nil, usage("operation schema requires NAME")
		}
		schema, ok := operation.Schema(args[1])
		if !ok {
			return nil, contract.NewError("not_found", "operation not found")
		}
		return map[string]any{"name": args[1], "schema_version": operation.OperationSchemaVersion, "input_schema": schema}, nil
	case "run":
		invocation, err := parseOperationRunArgs(args[1:])
		if err != nil {
			return nil, err
		}
		body, err := readJSONInput(r.Streams.In, invocation.InputPath)
		if err != nil {
			return nil, err
		}
		if r.Globals.RequestID != "" && (invocation.Name == "generate.image" || invocation.Name == "generate.video" || invocation.Name == "voice.convert") {
			body["request_id"] = r.Globals.RequestID
		} else if (invocation.Name == "generate.image" || invocation.Name == "generate.video" || invocation.Name == "voice.convert") && stringAny(body["request_id"], "") == "" {
			body["request_id"] = newRequestID()
		}
		return operation.Execute(ctx, operation.Runtime{
			Service: r.Service,
			OnEvent: r.Printer.Event,
			CopyAsset: func(ctx context.Context, source, destination string) (map[string]any, error) {
				return r.copyAssetValues(ctx, source, destination)
			},
		}, invocation.Name, body)
	default:
		return nil, usage("unknown operation command %q", args[0])
	}
}
