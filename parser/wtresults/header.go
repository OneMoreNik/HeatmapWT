package main

import (
	"bytes"
	"encoding/binary"
	"os"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl"
)

// readHeader parses just the fixed-size header of a replay file.
func readHeader(path string) (*wrpl.WRPLHeader, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	header := &wrpl.WRPLHeader{}
	if err := binary.Read(bytes.NewReader(raw), binary.LittleEndian, header); err != nil {
		return nil, err
	}
	return header, nil
}

// trimmed reads a NUL-padded fixed-size string field.
func trimmed(raw []byte) string {
	if i := bytes.IndexByte(raw, 0); i >= 0 {
		return string(raw[:i])
	}
	return string(raw)
}
