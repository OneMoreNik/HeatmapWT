package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"sort"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet"
)

var (
	flSpawn   = flag.Bool("spawn", false, "test whether the vec3 at -vec is a position, using the two spawn points")
	flVec     = flag.Int("vec", 27, "payload offset of the float32 triple to test")
	flSpawnAt = flag.Float64("spawn-at", 32, "seconds: sample every entity around this time, while everyone is still on their spawn")
	flWindow  = flag.Float64("window", 4, "seconds either side of -spawn-at to average over")
)

// Berlin's two Ground RB spawns, read from the mission blk by wtlevel. Every
// vehicle starts on one of them, so at match start the fleet forms two clusters
// a known distance apart. That distance calibrates any linear encoding.
var (
	spawnTeam1 = [2]float64{2389.1, 1604.6}
	spawnTeam2 = [2]float64{2383.1, 588.6}
)

// spawnTest averages each entity's candidate vector over a window at match
// start and reports the spread. If the vector is a position under any linear
// encoding, the entities must split into two clusters whose separation matches
// the real distance between the spawns; the ratio gives the scale.
func spawnTest(dir string) error {
	type acc struct {
		n    int
		sumX float64
		sumY float64
		sumZ float64
	}
	byEntity := map[uint64]*acc{}

	lo := *flSpawnAt - *flWindow
	hi := *flSpawnAt + *flWindow
	offset := *flVec

	_, _, err := eachPacket(dir, func(pk *packet.Packet) {
		if !isGroundUpdate(pk) || len(pk.PacketPayload) < offset+12 {
			return
		}
		t := float64(pk.CurrentTime) / 1000
		if t < lo || t > hi {
			return
		}
		id, ok := entityID(pk.PacketPayload)
		if !ok {
			return
		}
		p := pk.PacketPayload
		a := byEntity[id]
		if a == nil {
			a = &acc{}
			byEntity[id] = a
		}
		a.n++
		a.sumX += float64(math.Float32frombits(binary.LittleEndian.Uint32(p[offset:])))
		a.sumY += float64(math.Float32frombits(binary.LittleEndian.Uint32(p[offset+4:])))
		a.sumZ += float64(math.Float32frombits(binary.LittleEndian.Uint32(p[offset+8:])))
	})
	if err != nil {
		return err
	}
	if len(byEntity) == 0 {
		return fmt.Errorf("no updates between %.0fs and %.0fs", lo, hi)
	}

	ids := make([]uint64, 0, len(byEntity))
	for id := range byEntity {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })

	realSep := math.Hypot(spawnTeam1[0]-spawnTeam2[0], spawnTeam1[1]-spawnTeam2[1])
	fmt.Printf("%s\n", dir)
	fmt.Printf("  vec3 at payload offset %d, averaged over %.0f..%.0fs\n", offset, lo, hi)
	fmt.Printf("  real spawn separation: %.1f m\n", realSep)
	fmt.Printf("  %d entities present\n\n", len(ids))

	// Split on whichever axis has the widest spread; a spawn pair should
	// separate cleanly along one axis.
	var xs, ys, zs []float64
	fmt.Println("  entity        vx        vy        vz   samples")
	for _, id := range ids {
		a := byEntity[id]
		x, y, z := a.sumX/float64(a.n), a.sumY/float64(a.n), a.sumZ/float64(a.n)
		xs, ys, zs = append(xs, x), append(ys, y), append(zs, z)
		fmt.Printf("  %8d %9.3f %9.3f %9.3f %9d\n", id, x, y, z, a.n)
	}

	fmt.Println()
	for name, vals := range map[string][]float64{"vx": xs, "vy": ys, "vz": zs} {
		sorted := append([]float64(nil), vals...)
		sort.Float64s(sorted)
		span := sorted[len(sorted)-1] - sorted[0]
		// Largest gap between neighbouring values: a two-cluster split shows up
		// as one gap far wider than the rest.
		gap, at := 0.0, 0.0
		for i := 1; i < len(sorted); i++ {
			if d := sorted[i] - sorted[i-1]; d > gap {
				gap, at = d, (sorted[i]+sorted[i-1])/2
			}
		}
		below := 0
		for _, v := range sorted {
			if v < at {
				below++
			}
		}
		fmt.Printf("  %s: span %.3f, widest gap %.3f at %.3f, split %d/%d",
			name, span, gap, at, below, len(sorted)-below)
		if gap > 0 {
			fmt.Printf("  -> %.1f m per unit if that gap is the spawn separation", realSep/gap)
		}
		fmt.Println()
	}
	return nil
}
