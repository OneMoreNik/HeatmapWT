// Command wtcarve turns a downloaded server replay into flat files Python can
// read: one row per position sample, plus players, kills and a session summary.
//
// It runs wrpl-inspector's packet parsers itself rather than calling
// carve.CarveReplay, for two reasons:
//
//   - carve's own output marshals each path to a summary (first sample, last
//     sample, count), and the path itself is the whole point here;
//   - carve only keeps tracks whose entity the ECS parser managed to build.
//     ecshashes.json lags behind the game, so when it is stale every track is
//     dropped even though the position packets parsed fine. Here the raw
//     per-entity tracks are always written, and model / player / team are
//     filled in only where the ECS could resolve them.
//
//	wtcarve -out data/carved replays/server/<session hex>
package main

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/game"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet"
	packetdamage "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/damage"
	packetecs2 "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/ecs2"
	packetfm "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/fm"
	packetkill "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/kill"
	packetmovement "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/movement"
	packetnextsegment "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/nextSegment"
	packetslot "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/slot"
)

var (
	flECSHashes = flag.String("ecshashes", "vendor/wrpl-inspector/data/ecshashes.json", "path to ecshashes.json")
	flOut       = flag.String("out", "data/carved", "directory to write the carved session into")
	flErrors    = flag.Int("errors", 8, "how many parse errors to print")
)

func main() {
	flag.Parse()
	if flag.NArg() == 0 {
		fmt.Fprintln(os.Stderr, "usage: wtcarve [flags] <session dir holding 0000.wrpl...> ...")
		os.Exit(2)
	}

	hashesRaw, err := os.ReadFile(*flECSHashes)
	must(err, "reading ecshashes.json")
	hashes, err := packetecs2.ReadComponentHashMaps(bytes.NewReader(hashesRaw))
	must(err, "parsing ecshashes.json")

	for _, dir := range flag.Args() {
		if err := carveSession(dir, *hashes); err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", dir, err)
			os.Exit(1)
		}
	}
}

// openParts reads every NNNN.wrpl in dir, keyed by its part number.
func openParts(dir string) (map[int]*wrpl.ReplayReader, error) {
	paths, err := filepath.Glob(filepath.Join(dir, "*.wrpl"))
	if err != nil {
		return nil, err
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("no .wrpl parts found")
	}
	sort.Strings(paths)

	readers := map[int]*wrpl.ReplayReader{}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		reader, err := wrpl.OpenReplay(bytes.NewReader(raw), true, true, true)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", filepath.Base(path), err)
		}
		if !reader.Header.IsServer() {
			reader.Close()
			return nil, fmt.Errorf("%s: client replay, not a server part", filepath.Base(path))
		}
		readers[int(reader.Header.ReplayPartNumber)] = reader
	}
	return readers, nil
}

// carveResult is everything one session yields, before it is written out.
type carveResult struct {
	Header  wrpl.WRPLHeader
	Paths   map[uint64][]game.SpaceTime
	Air     *packetfm.PacketFlightModelParser
	Slots   [256]*packetslot.Player
	Kills   []packetkill.KillEntry
	ECS     *packetecs2.EntityManager
	Results []byte
	Errors  []string
}

func runParsers(readers map[int]*wrpl.ReplayReader, hashes packetecs2.ComponentHashMaps) (*carveResult, error) {
	parts := make([]int, 0, len(readers))
	for part := range readers {
		parts = append(parts, part)
	}
	sort.Ints(parts)
	if len(parts) == 0 || parts[0] != 0 {
		return nil, fmt.Errorf("no part 0 in the session")
	}

	nsp := &packetnextsegment.PacketNextSegmentParser{}
	ecsp := packetecs2.NewPacketECSParser(hashes)
	prp := packetmovement.NewPositionRetainerParser()
	fmp := &packetfm.PacketFlightModelParser{KeepResults: true, ECS: &ecsp.Mgr}
	kills := &packetkill.PacketKillParser{KeepKills: true, ECS: &ecsp.Mgr, PathsGround: prp, PathsAir: fmp}
	sltp := &packetslot.PacketSlotParser{}
	dcp := &packetdamage.CriticalDamageParser{KeepResults: true, ECS: &ecsp.Mgr}
	dsp := &packetdamage.SevereDamageParser{KeepResults: true, ECS: &ecsp.Mgr}
	matcher := packet.NewParserMatcher([]packet.PacketParser{nsp, prp, ecsp, sltp, kills, fmp, dcp, dsp})

	out := &carveResult{
		Header:  readers[parts[0]].Header,
		Air:     fmp,
		ECS:     &ecsp.Mgr,
		Results: readers[parts[len(parts)-1]].Results,
	}

	// A desync inside one part costs the rest of that part, not the session:
	// each part is a separate compressed stream, so the next one still reads.
	for _, part := range parts {
		reader := packet.NewPacketStreamReader(readers[part].PacketStream)
		pk := &packet.Packet{}
		for {
			isEOF, err := reader.ReadPacket(pk)
			if isEOF {
				break
			}
			if err != nil {
				out.Errors = append(out.Errors,
					fmt.Sprintf("part %d packet %d: %v", part, pk.Seq, err))
				break
			}
			for _, err := range matcher.MatchIgnoreData(pk) {
				out.Errors = append(out.Errors,
					fmt.Sprintf("part %d packet %d: %v", part, pk.Seq, err))
			}
			pk.Seq++
		}
	}

	out.Paths = prp.Paths
	out.Slots = sltp.Players
	out.Kills = kills.Kills
	return out, nil
}

func carveSession(dir string, hashes packetecs2.ComponentHashMaps) error {
	readers, err := openParts(dir)
	if err != nil {
		return err
	}
	defer func() {
		for _, r := range readers {
			r.Close()
		}
	}()

	res, err := runParsers(readers, hashes)
	if err != nil {
		return err
	}
	if *flECSDump > 0 {
		dumpECS(res)
		return nil
	}

	outDir := filepath.Join(*flOut, res.Header.SessionHEX())
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return err
	}

	players := collectPlayers(res)
	if err := writeJSON(filepath.Join(outDir, "players.json"), players); err != nil {
		return err
	}
	tracks, samples, err := writeTracks(outDir, res)
	if err != nil {
		return err
	}
	kills, err := writeKills(outDir, res)
	if err != nil {
		return err
	}
	if err := writeSummary(outDir, res, dir, len(readers), players, tracks, samples, kills); err != nil {
		return err
	}

	named := 0
	for _, t := range tracks {
		if t.Model != "" {
			named++
		}
	}
	fmt.Printf("%s\n", dir)
	fmt.Printf("  session      %s\n", res.Header.SessionHEX())
	fmt.Printf("  mission      %s | %s\n", trimmed(res.Header.Raw_Level[:]), trimmed(res.Header.Raw_BattleType[:]))
	fmt.Printf("  parts        %d\n", len(readers))
	fmt.Printf("  players      %d\n", len(players))
	fmt.Printf("  tracks       %d (%d with a resolved vehicle)\n", len(tracks), named)
	fmt.Printf("  samples      %d\n", samples)
	fmt.Printf("  kills        %d\n", kills)
	fmt.Printf("  ecs entities %d\n", len(res.ECS.Entities))
	fmt.Printf("  parse errors %d\n", len(res.Errors))
	for _, e := range res.Errors[:min(*flErrors, len(res.Errors))] {
		fmt.Printf("    %s\n", e)
	}
	fmt.Printf("  -> %s\n", outDir)
	return nil
}

type playerRow struct {
	Slot     int    `json:"slot"`
	PlayerID uint64 `json:"playerId"`
	Name     string `json:"name"`
	ClanTag  string `json:"clanTag"`
	Team     byte   `json:"team"`
}

func collectPlayers(res *carveResult) []playerRow {
	rows := []playerRow{}
	for slot, p := range res.Slots {
		if p == nil || p.Uid.Name == "" {
			continue
		}
		name := p.Uid.Name
		if p.RealNick != "" {
			name = p.RealNick
		}
		rows = append(rows, playerRow{
			Slot:     slot,
			PlayerID: p.Uid.Player_id,
			Name:     name,
			ClanTag:  p.ClanTag,
			Team:     p.Team,
		})
	}
	return rows
}

type trackRow struct {
	EntityID    uint64
	EntityIndex uint32
	PlayerID    uint64
	PlayerName  string
	Team        byte
	Model       string
	Samples     int
}

// resolveTrack maps a position-packet entity id onto an ECS entity. The
// shuffle is wrpl-inspector's: the movement packets carry the id rotated.
func resolveTrack(res *carveResult, eid uint64) (*packetecs2.Entity, uint32) {
	shuffled := ((eid&0xff)<<0x16 | eid>>0x8) & 0x7FF
	index := uint32(packetecs2.EntityID(uint32(shuffled)).Index())
	return res.ECS.Entities[index], index
}

func writeTracks(outDir string, res *carveResult) ([]trackRow, int, error) {
	w, fh, err := newCSV(filepath.Join(outDir, "tracks.csv"), []string{
		"entity_id", "player_id", "player_name", "team", "model", "time", "x", "y", "z",
	})
	if err != nil {
		return nil, 0, err
	}
	defer fh.Close()

	byName := map[uint64]string{}
	byTeam := map[uint64]byte{}
	for _, p := range res.Slots {
		if p == nil {
			continue
		}
		byName[p.Uid.Player_id] = p.Uid.Name
		byTeam[p.Uid.Player_id] = p.Team
	}

	eids := make([]uint64, 0, len(res.Paths))
	for eid := range res.Paths {
		eids = append(eids, eid)
	}
	sort.Slice(eids, func(i, j int) bool { return eids[i] < eids[j] })

	rows := []trackRow{}
	samples := 0
	for _, eid := range eids {
		path := res.Paths[eid]
		row := trackRow{EntityID: eid, Samples: len(path)}
		if entity, index := resolveTrack(res, eid); entity != nil {
			row.EntityIndex = index
			row.Model, _ = packetecs2.GetObjectData[string](&entity.Data, "unit__className")
			if slot, ok := packetecs2.GetObjectData[int32](&entity.Data, "unit__playerId"); ok {
				if slot >= 0 && int(slot) < len(res.Slots) && res.Slots[slot] != nil {
					row.PlayerID = res.Slots[slot].Uid.Player_id
					row.PlayerName = byName[row.PlayerID]
					row.Team = byTeam[row.PlayerID]
				}
			}
		}

		eidStr := strconv.FormatUint(eid, 10)
		pidStr := strconv.FormatUint(row.PlayerID, 10)
		teamStr := strconv.FormatUint(uint64(row.Team), 10)
		for _, p := range path {
			if err := w.Write([]string{
				eidStr, pidStr, row.PlayerName, teamStr, row.Model,
				strconv.FormatUint(uint64(p.Time), 10),
				strconv.FormatFloat(p.X, 'f', 2, 64),
				strconv.FormatFloat(p.Y, 'f', 2, 64),
				strconv.FormatFloat(p.Z, 'f', 2, 64),
			}); err != nil {
				return rows, samples, err
			}
			samples++
		}
		rows = append(rows, row)
	}
	w.Flush()
	return rows, samples, w.Error()
}

func writeKills(outDir string, res *carveResult) (int, error) {
	w, fh, err := newCSV(filepath.Join(outDir, "kills.csv"), []string{
		"time", "weapon", "killer_x", "killer_y", "killer_z", "victim_x", "victim_y", "victim_z",
	})
	if err != nil {
		return 0, err
	}
	defer fh.Close()

	written := 0
	for _, k := range res.Kills {
		if err := w.Write([]string{
			strconv.FormatUint(uint64(k.CurrentTime), 10),
			k.PlayerWeapon,
			axis(k.ResolvedKillerPosition, 0), axis(k.ResolvedKillerPosition, 1), axis(k.ResolvedKillerPosition, 2),
			axis(k.ResolvedVictimPosition, 0), axis(k.ResolvedVictimPosition, 1), axis(k.ResolvedVictimPosition, 2),
		}); err != nil {
			return written, err
		}
		written++
	}
	w.Flush()
	return written, w.Error()
}

func writeSummary(outDir string, res *carveResult, srcDir string, parts int,
	players []playerRow, tracks []trackRow, samples, kills int) error {
	type summary struct {
		SessionID     string     `json:"sessionId"`
		Source        string     `json:"source"`
		Parts         int        `json:"parts"`
		Version       int32      `json:"version"`
		Level         string     `json:"level"`
		LevelSettings string     `json:"levelSettings"`
		BattleType    string     `json:"battleType"`
		StartTime     uint32     `json:"startTime"`
		PlayerCount   int        `json:"playerCount"`
		TrackCount    int        `json:"trackCount"`
		SampleCount   int        `json:"sampleCount"`
		KillCount     int        `json:"killCount"`
		ECSEntities   int        `json:"ecsEntities"`
		Tracks        []trackRow `json:"tracks"`
		ParseErrors   []string   `json:"parseErrors"`
	}
	return writeJSON(filepath.Join(outDir, "session.json"), summary{
		SessionID:     res.Header.SessionHEX(),
		Source:        filepath.ToSlash(srcDir),
		Parts:         parts,
		Version:       res.Header.Version,
		Level:         trimmed(res.Header.Raw_Level[:]),
		LevelSettings: trimmed(res.Header.Raw_LevelSettings[:]),
		BattleType:    trimmed(res.Header.Raw_BattleType[:]),
		StartTime:     res.Header.StartTime,
		PlayerCount:   len(players),
		TrackCount:    len(tracks),
		SampleCount:   samples,
		KillCount:     kills,
		ECSEntities:   len(res.ECS.Entities),
		Tracks:        tracks,
		ParseErrors:   res.Errors,
	})
}

func trimmed(raw []byte) string {
	if i := bytes.IndexByte(raw, 0); i >= 0 {
		return string(raw[:i])
	}
	return string(raw)
}

func newCSV(path string, header []string) (*csv.Writer, *os.File, error) {
	fh, err := os.Create(path)
	if err != nil {
		return nil, nil, err
	}
	w := csv.NewWriter(fh)
	if err := w.Write(header); err != nil {
		fh.Close()
		return nil, nil, err
	}
	return w, fh, nil
}

func writeJSON(path string, v any) error {
	raw, err := json.MarshalIndent(v, "", "\t")
	if err != nil {
		return err
	}
	return os.WriteFile(path, raw, 0o644)
}

// axis formats one component of an optional position: 0=X, 1=Y, 2=Z.
func axis(p *game.SpaceTime, which int) string {
	if p == nil {
		return ""
	}
	v := p.Z
	switch which {
	case 0:
		v = p.X
	case 1:
		v = p.Y
	}
	return strconv.FormatFloat(v, 'f', 2, 64)
}

func must(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", what, err)
		os.Exit(1)
	}
}
