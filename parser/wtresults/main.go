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
	File string `json:"file"`
	// AuthorTeam is the side the recording player was on. A client replay can
	// only follow that side, and the client's own results screen lists only
	// that side, so it is the team a target player must be picked from.
	AuthorTeam int      `json:"authorTeam"`
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
		if fullName(out.Players[i]) == out.Author {
			out.AuthorTeam = out.Players[i].Team
		}
	}
	return out, nil
}

// fullName rebuilds the "CLAN Name" form the author field uses.
func fullName(p player) string {
	if p.ClanTag != "" {
		return p.ClanTag + " " + p.Name
	}
	return p.Name
}

// TopOfAuthorTeam is the highest scorer on the side the replay can follow.
func (b *battle) TopOfAuthorTeam() *player {
	for i := range b.Players {
		if b.Players[i].Team == b.AuthorTeam {
			return &b.Players[i]
		}
	}
	if len(b.Players) > 0 {
		return &b.Players[0]
	}
	return nil
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
		fmt.Printf("  %s | %s | session %s\n", b.Level, b.BattleType, b.SessionID)
		fmt.Printf("  author %s, team %d, %.0f s in the battle\n",
			b.Author, b.AuthorTeam, b.TimePlayed)
		if top := b.TopOfAuthorTeam(); top != nil {
			fmt.Printf("  follow: %s, %d points, top of the author's team\n",
				fullName(*top), top.Score)
		}
		shown := b.Players
		if *flTop > 0 && len(shown) > *flTop {
			shown = shown[:*flTop]
		}
		fmt.Printf("    %-4s %-26s %-5s %7s %6s %7s %7s\n",
			"rank", "player", "team", "score", "kills", "deaths", "assists")
		for _, p := range shown {
			// The client's own results screen lists only the author's team, so
			// mark it: those are the players a client replay can follow.
			mark := " "
			if p.Team == b.AuthorTeam {
				mark = "*"
			}
			fmt.Printf("  %s %-4d %-26s %-5d %7d %6d %7d %7d\n",
				mark, p.Rank, fullName(p), p.Team, p.Score, p.Kills, p.Deaths, p.Assists)
		}
		fmt.Println("    * = the author's team")
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
