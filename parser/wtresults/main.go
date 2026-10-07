// Command wtresults reads the scoreboard out of a replay file.
//
// Every .wrpl ends with a battle-results BLK in FAT form, which embeds its own
// names, so it parses from a client replay with nothing else needed. That is
// enough to decide who is worth following in a battle without watching it: the
// highest scorer is known before the replay is ever opened.
//
//	wtresults "replays/#2026.10.07 13.42.15.wrpl"
//	wtresults -json replays/*.wrpl
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl"
)

var (
	flJSON = flag.Bool("json", false, "write JSON instead of a table")
	flTop  = flag.Int("top", 0, "only show this many players")
)

type player struct {
	Name        string `json:"name"`
	ClanTag     string `json:"clanTag,omitempty"`
	UserID      string `json:"userId,omitempty"`
	Team        int    `json:"team"`
	Score       int    `json:"score"`
	Kills       int    `json:"kills"`
	GroundKills int    `json:"groundKills"`
	Deaths      int    `json:"deaths"`
	Assists     int    `json:"assists"`
	CaptureZone int    `json:"captureZone"`
	// Rank is the player's place on the scoreboard, 1 being the highest score.
	Rank int `json:"rank"`
}

type battle struct {
	File       string   `json:"file"`
	Level      string   `json:"level"`
	Mission    string   `json:"mission"`
	BattleType string   `json:"battleType"`
	SessionID  string   `json:"sessionId"`
	TimePlayed float64  `json:"timePlayed"`
	Author     string   `json:"author"`
	Players    []player `json:"players"`
}

// num reads a BLK value that may have been stored as any numeric type.
func num(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case float32:
		return float64(n)
	case int64:
		return float64(n)
	case int32:
		return float64(n)
	case int:
		return float64(n)
	case string:
		f, err := strconv.ParseFloat(n, 64)
		if err == nil {
			return f
		}
	}
	return 0
}

func str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func field(m map[string]any, key string) any {
	if v, ok := m[key]; ok {
		return v
	}
	return nil
}

func readBattle(path string) (*battle, error) {
	header, err := readHeader(path)
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if header.ResultsBlkOffset <= 0 || int(header.ResultsBlkOffset) >= len(raw) {
		return nil, fmt.Errorf("no results block")
	}
	results, err := wrpl.ParseBlk(raw[header.ResultsBlkOffset:])
	if err != nil {
		return nil, fmt.Errorf("parsing results: %w", err)
	}

	out := &battle{
		File:       path,
		Level:      trimmed(header.Raw_Level[:]),
		Mission:    trimmed(header.Raw_LevelSettings[:]),
		BattleType: trimmed(header.Raw_BattleType[:]),
		SessionID:  header.SessionHEX(),
		TimePlayed: num(field(results, "timePlayed")),
		Author:     str(field(results, "author")),
	}

	entries, _ := results["player"].([]any)
	for _, raw := range entries {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		name := str(field(entry, "name"))
		if name == "" {
			continue
		}
		out.Players = append(out.Players, player{
			Name:        name,
			ClanTag:     str(field(entry, "clanTag")),
			UserID:      str(field(entry, "userId")),
			Team:        int(num(field(entry, "team"))),
			Score:       int(num(field(entry, "score"))),
			Kills:       int(num(field(entry, "kills"))),
			GroundKills: int(num(field(entry, "groundKills"))),
			Deaths:      int(num(field(entry, "deaths"))),
			Assists:     int(num(field(entry, "assists"))),
			CaptureZone: int(num(field(entry, "captureZone"))),
		})
	}
	sort.SliceStable(out.Players, func(i, j int) bool {
		return out.Players[i].Score > out.Players[j].Score
	})
	for i := range out.Players {
		out.Players[i].Rank = i + 1
	}
	return out, nil
}

func main() {
	flag.Parse()
	if flag.NArg() == 0 {
		fmt.Fprintln(os.Stderr, "usage: wtresults [-json] [-top N] <replay.wrpl> ...")
		os.Exit(2)
	}

	var all []*battle
	for _, path := range flag.Args() {
		b, err := readBattle(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", path, err)
			continue
		}
		all = append(all, b)
		if *flJSON {
			continue
		}
		fmt.Printf("%s\n", path)
		fmt.Printf("  %s | %s | session %s | %.0f s played\n",
			b.Level, b.BattleType, b.SessionID, b.TimePlayed)
		shown := b.Players
		if *flTop > 0 && len(shown) > *flTop {
			shown = shown[:*flTop]
		}
		fmt.Printf("  %-4s %-26s %-5s %7s %6s %7s %7s\n",
			"rank", "player", "team", "score", "kills", "deaths", "assists")
		for _, p := range shown {
			name := p.Name
			if p.ClanTag != "" {
				name = p.ClanTag + " " + p.Name
			}
			fmt.Printf("  %-4d %-26s %-5d %7d %6d %7d %7d\n",
				p.Rank, name, p.Team, p.Score, p.Kills, p.Deaths, p.Assists)
		}
		fmt.Println()
	}

	if *flJSON {
		raw, err := json.MarshalIndent(all, "", "  ")
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Println(string(raw))
	}
}
