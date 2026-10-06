// Command wtlevel reads map alignment data straight out of an installed
// War Thunder client, so heatmaps can be placed on a map image without
// capturing anything from a running game.
//
// aces.vromfs.bin holds each level's blk, which carries tankMapCoord0 and
// tankMapCoord1: the world-space corners of the square the tank minimap covers.
// mis.vromfs.bin holds the mission blk per layout, whose areas give the capture
// points. Both are plain game content, read from disk.
//
//	wtlevel -game "D:/Games/WarThunder" -level levels/avg_berlin.bin \
//	        -mission gamedata/missions/cta/tanks/berlin/berlin_dom.blk
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wtcontent"
)

var (
	flGame    = flag.String("game", `D:/Games/WarThunder`, "War Thunder install directory")
	flLevel   = flag.String("level", "levels/avg_berlin.bin", "level path as it appears in the replay header")
	flMission = flag.String("mission", "", "mission blk path as it appears in the replay header")
	flOut     = flag.String("out", "", "write the result as JSON to this file")
	flList    = flag.String("list", "", "instead, list vromfs entries containing this substring")
)

// vromfs reads one packed game archive and returns its files plus name map.
func vromfs(name string) (*wtcontent.VROMFS, error) {
	raw, err := os.ReadFile(filepath.Join(*flGame, name))
	if err != nil {
		return nil, err
	}
	return wtcontent.ReadVROMFS(raw)
}

// archiveDict returns the zstd dictionary a vromfs ships for its BLKs, stored
// as a single "<sha256>.dict" entry at the archive root.
func archiveDict(v *wtcontent.VROMFS) []byte {
	for name, body := range v.Files {
		if strings.HasSuffix(name, ".dict") && !strings.Contains(name, "/") {
			return body
		}
	}
	return nil
}

// parseBLK decodes a binary blk from a vromfs. Level and mission BLKs are
// SLIM_ZSTD_DICT, so they need both the archive's name map and its dictionary.
func parseBLK(v *wtcontent.VROMFS, path string) (map[string]any, error) {
	body, ok := v.Files[path]
	if !ok {
		return nil, fmt.Errorf("%q not in archive", path)
	}
	nameMapRaw, ok := v.Files["\xff?nm"]
	if !ok {
		return wrpl.ParseBlk(body)
	}
	nameMap, err := wrpl.ParseNameMap(nameMapRaw)
	if err != nil {
		return nil, fmt.Errorf("parsing name map: %w", err)
	}
	return wrpl.ParseBlkWithNameMapAndDict(body, nameMap, archiveDict(v))
}

type levelBounds struct {
	Level string    `json:"level"`
	Min   []float64 `json:"tankMapCoord0"`
	Max   []float64 `json:"tankMapCoord1"`
	// SizeMetres is the span the minimap covers, handy as a sanity check.
	SizeMetres []float64 `json:"sizeMetres"`
}

type captureArea struct {
	Name string    `json:"name"`
	Type string    `json:"type"`
	X    float64   `json:"x"`
	Z    float64   `json:"z"`
	TM   []float64 `json:"tm,omitempty"`
}

type result struct {
	Bounds   *levelBounds  `json:"bounds,omitempty"`
	Mission  string        `json:"mission,omitempty"`
	Captures []captureArea `json:"captures,omitempty"`
}

func floats(v any) []float64 {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]float64, 0, len(raw))
	for _, item := range raw {
		f, ok := item.(float64)
		if !ok {
			return nil
		}
		out = append(out, f)
	}
	return out
}

// rowsOfFloats reads a matrix stored as an array of float arrays.
func rowsOfFloats(v any) [][]float64 {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	rows := make([][]float64, 0, len(raw))
	for _, item := range raw {
		row := floats(item)
		if row == nil {
			return nil
		}
		rows = append(rows, row)
	}
	return rows
}

func listEntries(substr string) error {
	for _, archive := range []string{"aces.vromfs.bin", "mis.vromfs.bin"} {
		v, err := vromfs(archive)
		if err != nil {
			fmt.Fprintf(os.Stderr, "  %s: %v\n", archive, err)
			continue
		}
		names := make([]string, 0, len(v.Files))
		for name := range v.Files {
			if strings.Contains(strings.ToLower(name), strings.ToLower(substr)) {
				names = append(names, name)
			}
		}
		sort.Strings(names)
		fmt.Printf("%s: %d of %d entries match %q\n", archive, len(names), len(v.Files), substr)
		for _, name := range names[:min(40, len(names))] {
			fmt.Printf("  %s (%d bytes)\n", name, len(v.Files[name]))
		}
	}
	return nil
}

func readBounds() (*levelBounds, error) {
	aces, err := vromfs("aces.vromfs.bin")
	if err != nil {
		return nil, err
	}
	path := strings.TrimSuffix(*flLevel, ".bin") + ".blk"
	blk, err := parseBLK(aces, path)
	if err != nil {
		return nil, err
	}
	lo, hi := floats(blk["tankMapCoord0"]), floats(blk["tankMapCoord1"])
	if len(lo) < 2 || len(hi) < 2 {
		keys := make([]string, 0, len(blk))
		for k := range blk {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		return nil, fmt.Errorf("%s has no tankMapCoord0/1; keys: %s", path, strings.Join(keys, " "))
	}
	return &levelBounds{
		Level: *flLevel, Min: lo, Max: hi,
		SizeMetres: []float64{abs(hi[0] - lo[0]), abs(hi[1] - lo[1])},
	}, nil
}

func abs(v float64) float64 {
	if v < 0 {
		return -v
	}
	return v
}

func readCaptures() ([]captureArea, error) {
	mis, err := vromfs("mis.vromfs.bin")
	if err != nil {
		return nil, err
	}
	blk, err := parseBLK(mis, *flMission)
	if err != nil {
		return nil, err
	}
	areas, ok := blk["areas"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s has no areas block", *flMission)
	}
	var out []captureArea
	for name, raw := range areas {
		area, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		kind, _ := area["type"].(string)
		tm := floats(area["tm"])
		entry := captureArea{Name: name, Type: kind, TM: tm}
		// An area's tm is a 4x3 matrix stored as four rows; the last row is
		// the translation, so its X and Z are the area's position on the map.
		if rows := rowsOfFloats(area["tm"]); len(rows) >= 4 && len(rows[3]) >= 3 {
			entry.X, entry.Z = rows[3][0], rows[3][2]
			entry.TM = nil
		}
		out = append(out, entry)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func main() {
	flag.Parse()

	if *flList != "" {
		if err := listEntries(*flList); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	var res result
	bounds, err := readBounds()
	if err != nil {
		fmt.Fprintf(os.Stderr, "bounds: %v\n", err)
	} else {
		res.Bounds = bounds
		fmt.Printf("%s\n", bounds.Level)
		fmt.Printf("  tankMapCoord0 %v\n", bounds.Min)
		fmt.Printf("  tankMapCoord1 %v\n", bounds.Max)
		fmt.Printf("  covers        %.0f x %.0f m\n", bounds.SizeMetres[0], bounds.SizeMetres[1])
	}

	if *flMission != "" {
		captures, err := readCaptures()
		if err != nil {
			fmt.Fprintf(os.Stderr, "captures: %v\n", err)
		} else {
			res.Mission = *flMission
			res.Captures = captures
			fmt.Printf("%s: %d areas\n", *flMission, len(captures))
			for _, c := range captures {
				fmt.Printf("  %-40s %-16s x=%8.1f z=%8.1f\n", c.Name, c.Type, c.X, c.Z)
			}
		}
	}

	if *flOut != "" {
		raw, err := json.MarshalIndent(res, "", "\t")
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if err := os.WriteFile(*flOut, raw, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Printf("-> %s\n", *flOut)
	}
}
