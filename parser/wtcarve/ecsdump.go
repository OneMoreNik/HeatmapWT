package main

import (
	"flag"
	"fmt"
	"sort"

	packetecs2 "github.com/maxsupermanhd/wrpl-inspector/v3/wrpl/packet/parser/ecs2"
)

var flECSDump = flag.Int("ecsdump", 0, "print the components of this many ECS entities and exit")

// dumpECS reports which components the ECS parser actually managed to decode.
// It answers whether vehicle identity (unit__className) and any transform are
// reachable through the ECS, or whether ecshashes.json is too stale to help.
func dumpECS(res *carveResult) {
	indexes := make([]uint32, 0, len(res.ECS.Entities))
	for idx, e := range res.ECS.Entities {
		if e != nil {
			indexes = append(indexes, idx)
		}
	}
	sort.Slice(indexes, func(i, j int) bool { return indexes[i] < indexes[j] })

	// How often each component name appears across all entities.
	counts := map[string]int{}
	templates := map[string]int{}
	compCounts := map[int]int{}
	withComponents, withClassName := 0, 0
	for _, idx := range indexes {
		e := res.ECS.Entities[idx]
		templates[e.Template]++
		compCounts[len(e.Data.Components)]++
		if len(e.Data.Components) > 0 {
			withComponents++
		}
		for _, name := range componentNames(e) {
			counts[name]++
			if name == "unit__className" {
				withClassName++
			}
		}
	}

	fmt.Printf("  ECS entities parsed:                     %d\n", len(indexes))
	fmt.Printf("  entities with at least one component:    %d\n", withComponents)
	fmt.Printf("  entities carrying unit__className:       %d\n", withClassName)

	fmt.Println("  component count per entity:")
	sizes := make([]int, 0, len(compCounts))
	for n := range compCounts {
		sizes = append(sizes, n)
	}
	sort.Ints(sizes)
	for _, n := range sizes[:min(10, len(sizes))] {
		fmt.Printf("    %3d components  x%d\n", n, compCounts[n])
	}

	fmt.Println("  most common templates (names come straight off the wire):")
	tnames := make([]string, 0, len(templates))
	for t := range templates {
		tnames = append(tnames, t)
	}
	sort.Slice(tnames, func(i, j int) bool { return templates[tnames[i]] > templates[tnames[j]] })
	for _, t := range tnames[:min(20, len(tnames))] {
		fmt.Printf("    %5d  %s\n", templates[t], t)
	}
	fmt.Println("  most common component names:")
	names := make([]string, 0, len(counts))
	for n := range counts {
		names = append(names, n)
	}
	sort.Slice(names, func(i, j int) bool {
		if counts[names[i]] != counts[names[j]] {
			return counts[names[i]] > counts[names[j]]
		}
		return names[i] < names[j]
	})
	for _, n := range names[:min(40, len(names))] {
		fmt.Printf("    %6d  %s\n", counts[n], n)
	}

	shown := 0
	for _, idx := range indexes {
		if shown >= *flECSDump {
			break
		}
		e := res.ECS.Entities[idx]
		class, _ := packetecs2.GetObjectData[string](&e.Data, "unit__className")
		if class == "" {
			continue
		}
		fmt.Printf("  entity index %d  class=%q\n", idx, class)
		for _, name := range componentNames(e) {
			fmt.Printf("    %s\n", name)
		}
		shown++
	}
}

// componentNames lists the decoded component names of one entity.
func componentNames(e *packetecs2.Entity) []string {
	names := make([]string, 0, len(e.Data.Components))
	for _, c := range e.Data.Components {
		names = append(names, c.Name)
	}
	sort.Strings(names)
	return names
}
