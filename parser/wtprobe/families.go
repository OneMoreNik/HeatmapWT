package main

import (
	"flag"
	"fmt"
	"sort"

	"github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet"
)

var (
	flFamilies = flag.Bool("families", false, "group packets by type, length and leading bytes")
	flSigLen   = flag.Int("siglen", 11, "how many leading payload bytes form the signature")
)

// family identifies a kind of packet by its shape rather than its contents.
type family struct {
	packetType byte
	length     int
	signature  string
}

type familyStats struct {
	count     int
	entities  map[uint64]struct{}
	firstTime uint32
	lastTime  uint32
}

// families groups every packet by type, payload length and leading bytes. A
// per-vehicle state stream stands out as a family with roughly one entity per
// player, a steady rate, and a fixed payload length.
func familyReport(dir string) error {
	stats := map[family]*familyStats{}

	parts, packets, err := eachPacket(dir, func(pk *packet.Packet) {
		payload := pk.PacketPayload
		sigLen := min(*flSigLen, len(payload))
		f := family{
			packetType: pk.PacketType,
			length:     len(payload),
			signature:  fmt.Sprintf("% x", payload[:sigLen]),
		}
		s := stats[f]
		if s == nil {
			s = &familyStats{entities: map[uint64]struct{}{}, firstTime: pk.CurrentTime}
			stats[f] = s
		}
		s.count++
		s.lastTime = pk.CurrentTime
		if len(payload) > 4 {
			if id, ok := entityID(payload); ok {
				s.entities[id] = struct{}{}
			}
		}
	})
	if err != nil {
		return err
	}

	// Signatures differ in their entity-id bytes, so collapse families that
	// share a type and length but differ only inside the varint id region.
	type shape struct {
		packetType byte
		length     int
	}
	shapes := map[shape]*familyStats{}
	for f, s := range stats {
		k := shape{f.packetType, f.length}
		agg := shapes[k]
		if agg == nil {
			agg = &familyStats{entities: map[uint64]struct{}{}, firstTime: s.firstTime}
			shapes[k] = agg
		}
		agg.count += s.count
		for id := range s.entities {
			agg.entities[id] = struct{}{}
		}
		if s.firstTime < agg.firstTime {
			agg.firstTime = s.firstTime
		}
		if s.lastTime > agg.lastTime {
			agg.lastTime = s.lastTime
		}
	}

	fmt.Printf("%s: %d parts, %d packets, %d shapes\n", dir, parts, packets, len(shapes))
	keys := make([]shape, 0, len(shapes))
	for k := range shapes {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return shapes[keys[i]].count > shapes[keys[j]].count })

	fmt.Println("  type  len   packets  entities   span(s)   per entity per second")
	for _, k := range keys[:min(30, len(keys))] {
		s := shapes[k]
		span := float64(s.lastTime-s.firstTime) / 1000
		rate := 0.0
		if span > 0 && len(s.entities) > 0 {
			rate = float64(s.count) / span / float64(len(s.entities))
		}
		fmt.Printf("  %4d %4d %9d %9d %9.1f %12.2f\n",
			k.packetType, k.length, s.count, len(s.entities), span, rate)
	}
	return nil
}
