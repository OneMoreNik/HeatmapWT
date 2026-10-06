package main

import (
	"bytes"
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"sort"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/danet"
	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet"
)

var (
	flDump    = flag.Uint64("dump", 0, "dump raw payloads for this entity id (from -tracks)")
	flDumpN   = flag.Int("dump-n", 12, "how many payloads to print, spread across the session")
	flColumns = flag.Bool("columns", false, "with -dump, rank byte offsets by how much they look like a coordinate")
	flAfter   = flag.Float64("after", 0, "ignore updates before this time in seconds")
)

// entityID reads the per-update entity id: a varint at payload[2:]. It is
// stable for the life of a vehicle, so it identifies one track.
func entityID(payload []byte) (uint64, bool) {
	v, err := danet.NewBitReader(payload[2:]).ReadCompressed()
	return v, err == nil
}

// isGroundUpdate reports whether a packet is a per-entity movement update.
func isGroundUpdate(pk *packet.Packet) bool {
	return pk.PacketType == 4 &&
		len(pk.PacketPayload) >= groundPositionOffset+24 &&
		bytes.Equal(pk.PacketPayload[5:11], groundPositionMarker)
}

type dumpSample struct {
	time    uint32
	payload []byte
}

// column describes how one candidate float field behaves over a whole track.
type column struct {
	offset  int
	width   int
	lo, hi  float64
	medStep float64
	maxStep float64
	changed float64 // share of consecutive pairs that differ at all
}

// score ranks a column by how much it looks like a world coordinate: a wide
// range, a magnitude that fits a map, and steps a tank could actually make.
func (c column) score() float64 {
	span := c.hi - c.lo
	if span < 5 || math.Abs(c.lo) > 8192 || math.Abs(c.hi) > 8192 {
		return 0
	}
	if c.maxStep > 40 { // 8 Hz updates, so 40 m between ticks is a teleport
		return 0
	}
	return span * c.changed
}

func collectColumns(samples []dumpSample) []column {
	shortest := len(samples[0].payload)
	for _, s := range samples {
		shortest = min(shortest, len(s.payload))
	}

	var cols []column
	for _, width := range []int{4, 8} {
		for off := 0; off+width <= shortest; off++ {
			values := make([]float64, 0, len(samples))
			for _, s := range samples {
				var v float64
				if width == 4 {
					v = float64(math.Float32frombits(binary.LittleEndian.Uint32(s.payload[off:])))
				} else {
					v = math.Float64frombits(binary.LittleEndian.Uint64(s.payload[off:]))
				}
				values = append(values, v)
			}

			c := column{offset: off, width: width, lo: math.Inf(1), hi: math.Inf(-1)}
			steps := make([]float64, 0, len(values))
			changes, pairs, bad := 0, 0, false
			for i, v := range values {
				if math.IsNaN(v) || math.IsInf(v, 0) {
					bad = true
					break
				}
				c.lo, c.hi = math.Min(c.lo, v), math.Max(c.hi, v)
				if i > 0 {
					step := math.Abs(v - values[i-1])
					steps = append(steps, step)
					c.maxStep = math.Max(c.maxStep, step)
					pairs++
					if step > 0 {
						changes++
					}
				}
			}
			if bad || pairs == 0 {
				continue
			}
			sort.Float64s(steps)
			c.medStep = steps[len(steps)/2]
			c.changed = float64(changes) / float64(pairs)
			cols = append(cols, c)
		}
	}
	return cols
}

// dump prints raw payloads for one entity plus a ranking of its float columns.
func dump(dir string) error {
	var samples []dumpSample
	lengths := map[int]int{}

	_, _, err := eachPacket(dir, func(pk *packet.Packet) {
		if !isGroundUpdate(pk) {
			return
		}
		if float64(pk.CurrentTime)/1000 < *flAfter {
			return
		}
		if id, ok := entityID(pk.PacketPayload); !ok || id != *flDump {
			return
		}
		lengths[len(pk.PacketPayload)]++
		samples = append(samples, dumpSample{
			time:    pk.CurrentTime,
			payload: bytes.Clone(pk.PacketPayload),
		})
	})
	if err != nil {
		return err
	}
	if len(samples) == 0 {
		return fmt.Errorf("no movement updates for entity %d after %.0fs", *flDump, *flAfter)
	}

	fmt.Printf("entity %d: %d updates from %.1fs to %.1fs\n",
		*flDump, len(samples), float64(samples[0].time)/1000, float64(samples[len(samples)-1].time)/1000)
	fmt.Print("  payload lengths:")
	lens := make([]int, 0, len(lengths))
	for l := range lengths {
		lens = append(lens, l)
	}
	sort.Ints(lens)
	for _, l := range lens {
		fmt.Printf(" %d(x%d)", l, lengths[l])
	}
	fmt.Println()

	stride := max(1, len(samples)/max(1, *flDumpN))
	for i := 0; i < len(samples) && i/stride < *flDumpN; i += stride {
		s := samples[i]
		fmt.Printf("  t=%7.2fs  % x\n", float64(s.time)/1000, s.payload)
	}
	if !*flColumns {
		return nil
	}

	cols := collectColumns(samples)
	sort.Slice(cols, func(i, j int) bool { return cols[i].score() > cols[j].score() })
	fmt.Println("  float columns most like a world coordinate:")
	fmt.Println("    offset  type       range                 median step  max step  changes")
	shown := 0
	for _, c := range cols {
		if c.score() == 0 || shown >= 14 {
			break
		}
		fmt.Printf("    %4d    f%-2d   %9.1f .. %-9.1f   %9.3f %9.2f   %5.0f%%\n",
			c.offset, c.width*8, c.lo, c.hi, c.medStep, c.maxStep, c.changed*100)
		shown++
	}
	if shown == 0 {
		fmt.Println("    none: no column has a map-sized range with tank-sized steps")
	}
	return nil
}
