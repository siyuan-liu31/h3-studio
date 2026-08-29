package output

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"text/tabwriter"

	"h3studio/cli/internal/contract"
)

type Printer struct {
	Stdout, Stderr io.Writer
	Format         string
	Quiet          bool
}

func (p Printer) Result(command string, data any) error {
	if p.Format == "json" || p.Format == "jsonl" {
		return contract.WriteJSON(p.Stdout, contract.Success(command, data))
	}
	return writeTable(p.Stdout, data)
}

func writeTable(w io.Writer, data any) error {
	table := tabwriter.NewWriter(w, 0, 4, 2, ' ', 0)
	defer table.Flush()
	if object, ok := data.(map[string]any); ok {
		for _, value := range object {
			if rows, ok := value.([]any); ok && len(rows) > 0 {
				keys := []string{}
				seen := map[string]bool{}
				for _, raw := range rows {
					if row, ok := raw.(map[string]any); ok {
						for key, cell := range row {
							if scalar(cell) && !seen[key] {
								seen[key] = true
								keys = append(keys, key)
							}
						}
					}
				}
				sort.Strings(keys)
				if len(keys) > 0 {
					fmt.Fprintln(table, strings.ToUpper(strings.Join(keys, "\t")))
					for _, raw := range rows {
						row, _ := raw.(map[string]any)
						cells := make([]string, len(keys))
						for i, key := range keys {
							cells[i] = format(row[key])
						}
						fmt.Fprintln(table, strings.Join(cells, "\t"))
					}
					return nil
				}
			}
		}
		keys := make([]string, 0, len(object))
		for key := range object {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			fmt.Fprintf(table, "%s\t%s\n", key, format(object[key]))
		}
		return nil
	}
	_, err := fmt.Fprintln(table, format(data))
	return err
}

func scalar(value any) bool {
	switch value.(type) {
	case nil, string, bool, float64, float32, int, int64, int32, json.Number:
		return true
	}
	return false
}
func format(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	if scalar(value) {
		return fmt.Sprint(value)
	}
	raw, _ := json.Marshal(value)
	return string(raw)
}

func (p Printer) Event(event map[string]any) {
	if p.Format == "jsonl" {
		_ = contract.WriteJSON(p.Stdout, event)
		return
	}
	if !p.Quiet {
		status, _ := event["status"].(string)
		if status != "" {
			fmt.Fprintf(p.Stderr, "job status: %s\n", status)
		}
	}
}

func (p Printer) Failure(command string, err error) {
	if p.Format == "json" || p.Format == "jsonl" {
		_ = contract.WriteJSON(p.Stdout, contract.ErrorEnvelope(command, err))
		return
	}
	fmt.Fprintf(p.Stderr, "error: %s\n", err)
}
