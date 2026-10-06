// Command wtprobe locates the packets that carry ground vehicle positions.
//
// wrpl-inspector matches those packets by a byte signature that no longer fits
// game version 101404, so this looks for the thing itself rather than trusting
// a signature: a triple of consecutive little-endian floats that could be a
// point on the map, with a believable altitude.
//
// Two modes:
//
//	wtprobe -parts 4 <session dir>            histogram of where triples sit
//	wtprobe -tracks -parts 4 <session dir>    group them into per-entity tracks
//
// The histogram over a Berlin session points at packet type 4, a float64
// triple at payload offset 11, behind the constant marker at payload[5:11]
// below. The tracks mode then checks that grouping those samples by entity id
// gives one coherent track per vehicle.
package main

import (
	"bytes"
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/danet"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet"
)

var (
	flBound  = flag.Float64("bound", 8192, "largest plausible |x| or |z| in metres")
	flMinAlt = flag.Float64("alt-min", -200, "lowest plausible altitude in metres")
	flMaxAlt = flag.Float64("alt-max", 1500, "highest plausible altitude in metres")
	flMinRad = flag.Float64("min-radius", 150, "smallest plausible distance from the map centre in metres")
	flTop    = flag.Int("top", 25, "how many signatures to print")
	flParts  = flag.Int("parts", 0, "stop after this many parts (0 = all)")
	flF32    = flag.Bool("f32", false, "look for float32 triples instead of float64")
	flTracks = flag.Bool("tracks", false, "group marker-matched samples into per-entity tracks")
)

// groundPositionMarker sits at payload[5:11] of every ground position packet
// seen in game version 101404; payload[11:35] is then the float64 X, Y, Z.
var groundPositionMarker = []byte{0xcc, 0xf0, 0x35, 0x00, 0xfe, 0x01}

const groundPositionOffset = 11

func main() {
	flag.Parse()
	if flag.NArg() == 0 {
		fmt.Fprintln(os.Stderr, "usage: wtprobe [flags] <session dir> ...")
		os.Exit(2)
	}
	run := histogram
	switch {
	case *flFamilies:
		run = familyReport
	case *flSpawn:
		run = spawnTest
	case *flDump > 0:
		run = dump
	case *flTracks:
		run = tracks
	}
	for _, dir := range flag.Args() {
		if err := run(dir); err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", dir, err)
			os.Exit(1)
		}
	}
}

// eachPacket walks every packet of every part of a session.
func eachPacket(dir string, visit func(pk *packet.Packet)) (parts, packets int, err error) {
	paths, err := filepath.Glob(filepath.Join(dir, "*.wrpl"))
	if err != nil {
		return 0, 0, err
	}
	sort.Strings(paths)
	if *flParts > 0 && len(paths) > *flParts {
		paths = paths[:*flParts]
	}

	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return len(paths), packets, err
		}
		reader, err := wrpl.OpenReplay(bytes.NewReader(raw), false, true, false)
		if err != nil {
			return len(paths), packets, fmt.Errorf("%s: %w", filepath.Base(path), err)
		}
		stream := packet.NewPacketStreamReader(reader.PacketStream)
		pk := &packet.Packet{}
		for {
			isEOF, err := stream.ReadPacket(pk)
			if isEOF || err != nil {
				break
			}
			packets++
			visit(pk)
		}
		reader.Close()
	}
	return len(paths), packets, nil
}

func plausible(x, y, z float64) bool {
	if math.IsNaN(x) || math.IsNaN(y) || math.IsNaN(z) {
		return false
	}
	if math.Abs(x) > *flBound || math.Abs(z) > *flBound {
		return false
	}
	if y < *flMinAlt || y > *flMaxAlt {
		return false
	}
	// Local-space values (velocities, offsets, normals) cluster near zero; a
	// real world position on a 4 km map is hundreds of metres from the centre.
	return math.Hypot(x, z) >= *flMinRad
}

// hit is one (packet type, triple offset) pair with its leading payload bytes.
type hit struct {
	packetType byte
	offset     int
	prefix     string
}

func histogram(dir string) error {
	typeCounts := map[byte]int{}
	typeHits := map[byte]int{}
	sigCounts := map[hit]int{}
	offsetCounts := map[int]int{}

	width := 8
	if *flF32 {
		width = 4
	}
	read := func(b []byte) float64 {
		if *flF32 {
			return float64(math.Float32frombits(binary.LittleEndian.Uint32(b)))
		}
		return math.Float64frombits(binary.LittleEndian.Uint64(b))
	}

	parts, packets, err := eachPacket(dir, func(pk *packet.Packet) {
		typeCounts[pk.PacketType]++
		payload := pk.PacketPayload
		for off := 0; off+3*width <= len(payload); off++ {
			x, y, z := read(payload[off:]), read(payload[off+width:]), read(payload[off+2*width:])
			if !plausible(x, y, z) {
				continue
			}
			typeHits[pk.PacketType]++
			offsetCounts[off]++
			prefixLen := min(off, 8)
			sigCounts[hit{
				packetType: pk.PacketType,
				offset:     off,
				prefix:     fmt.Sprintf("% x", payload[off-prefixLen:off]),
			}]++
			return
		}
	})
	if err != nil {
		return err
	}

	fmt.Printf("%s: %d parts, %d packets\n", dir, parts, packets)
	fmt.Println("  packets by type (hits = contain a plausible position triple):")
	types := make([]int, 0, len(typeCounts))
	for t := range typeCounts {
		types = append(types, int(t))
	}
	sort.Ints(types)
	for _, t := range types {
		fmt.Printf("    type %3d  %8d packets  %8d hits\n", t, typeCounts[byte(t)], typeHits[byte(t)])
	}

	fmt.Println("  triple offsets within the payload:")
	offsets := make([]int, 0, len(offsetCounts))
	for o := range offsetCounts {
		offsets = append(offsets, o)
	}
	sort.Slice(offsets, func(i, j int) bool { return offsetCounts[offsets[i]] > offsetCounts[offsets[j]] })
	for _, o := range offsets[:min(12, len(offsets))] {
		fmt.Printf("    offset %3d  %8d\n", o, offsetCounts[o])
	}

	fmt.Printf("  top %d signatures (type, offset, 8 bytes before the triple):\n", *flTop)
	sigs := make([]hit, 0, len(sigCounts))
	for s := range sigCounts {
		sigs = append(sigs, s)
	}
	sort.Slice(sigs, func(i, j int) bool { return sigCounts[sigs[i]] > sigCounts[sigs[j]] })
	for _, s := range sigs[:min(*flTop, len(sigs))] {
		fmt.Printf("    type %3d  offset %3d  [%s]  %8d\n", s.packetType, s.offset, s.prefix, sigCounts[s])
	}
	return nil
}

// trackStats is what one candidate entity's samples look like in aggregate.
type trackStats struct {
	samples             int
	minX, maxX          float64
	minY, maxY          float64
	minZ, maxZ          float64
	firstTime, lastTime uint32
	maxSpeed            float64
	lastX, lastZ        float64
}

func (st *trackStats) add(x, y, z float64, t uint32) {
	if st.samples == 0 {
		st.minX, st.maxX = x, x
		st.minY, st.maxY = y, y
		st.minZ, st.maxZ = z, z
		st.firstTime = t
	} else if t > st.lastTime {
		speed := math.Hypot(x-st.lastX, z-st.lastZ) / (float64(t-st.lastTime) / 1000)
		st.maxSpeed = math.Max(st.maxSpeed, speed)
	}
	st.samples++
	st.lastTime, st.lastX, st.lastZ = t, x, z
	st.minX, st.maxX = math.Min(st.minX, x), math.Max(st.maxX, x)
	st.minY, st.maxY = math.Min(st.minY, y), math.Max(st.maxY, y)
	st.minZ, st.maxZ = math.Min(st.minZ, z), math.Max(st.maxZ, z)
}

// tracks groups marker-matched samples by several candidate entity-id
// encodings, so the one that yields a coherent track per vehicle is visible.
func tracks(dir string) error {
	type candidate struct {
		name string
		id   func(payload []byte) (uint64, bool)
	}
	candidates := []candidate{
		{"varint@2", func(p []byte) (uint64, bool) {
			v, err := danet.NewBitReader(p[2:]).ReadCompressed()
			return v, err == nil
		}},
		{"u16@3", func(p []byte) (uint64, bool) {
			return uint64(binary.LittleEndian.Uint16(p[3:])), true
		}},
		{"u16@3>>3", func(p []byte) (uint64, bool) {
			return uint64(binary.LittleEndian.Uint16(p[3:])) >> 3, true
		}},
		{"u24@2", func(p []byte) (uint64, bool) {
			return uint64(p[2]) | uint64(p[3])<<8 | uint64(p[4])<<16, true
		}},
	}
	groups := make([]map[uint64]*trackStats, len(candidates))
	for i := range groups {
		groups[i] = map[uint64]*trackStats{}
	}

	matched, inMap := 0, 0
	parts, packets, err := eachPacket(dir, func(pk *packet.Packet) {
		payload := pk.PacketPayload
		if pk.PacketType != 4 || len(payload) < groundPositionOffset+24 {
			return
		}
		if !bytes.Equal(payload[5:11], groundPositionMarker) {
			return
		}
		matched++
		x := math.Float64frombits(binary.LittleEndian.Uint64(payload[groundPositionOffset:]))
		y := math.Float64frombits(binary.LittleEndian.Uint64(payload[groundPositionOffset+8:]))
		z := math.Float64frombits(binary.LittleEndian.Uint64(payload[groundPositionOffset+16:]))
		if math.IsNaN(x) || math.IsNaN(y) || math.IsNaN(z) {
			return
		}
		if math.Abs(x) > *flBound || math.Abs(z) > *flBound || y < *flMinAlt || y > *flMaxAlt {
			return
		}
		inMap++
		for i, c := range candidates {
			if id, ok := c.id(payload); ok {
				st := groups[i][id]
				if st == nil {
					st = &trackStats{}
					groups[i][id] = st
				}
				st.add(x, y, z, pk.CurrentTime)
			}
		}
	})
	if err != nil {
		return err
	}

	fmt.Printf("%s: %d parts, %d packets, %d matched the marker, %d inside the map\n",
		dir, parts, packets, matched, inMap)
	for i, c := range candidates {
		fmt.Printf("  entity id as %-10s -> %4d groups\n", c.name, len(groups[i]))
	}

	// Pick the encoding whose group count is closest to a full battle roster.
	best, bestScore := 0, math.Inf(1)
	for i := range candidates {
		if score := math.Abs(float64(len(groups[i])) - 32); score < bestScore {
			best, bestScore = i, score
		}
	}
	fmt.Printf("  detail for %s:\n", candidates[best].name)
	ids := make([]uint64, 0, len(groups[best]))
	for id := range groups[best] {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(a, b int) bool { return groups[best][ids[a]].samples > groups[best][ids[b]].samples })
	fmt.Println("    entity   samples   x range            y range        z range            t range (s)  max m/s")
	for _, id := range ids[:min(45, len(ids))] {
		st := groups[best][id]
		fmt.Printf("    %8d %8d  %7.0f..%-7.0f %5.0f..%-5.0f %7.0f..%-7.0f %5.0f..%-5.0f %8.1f\n",
			id, st.samples, st.minX, st.maxX, st.minY, st.maxY, st.minZ, st.maxZ,
			float64(st.firstTime)/1000, float64(st.lastTime)/1000, st.maxSpeed)
	}
	return nil
}
