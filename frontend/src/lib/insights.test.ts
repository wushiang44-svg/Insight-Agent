import { describe, expect, it } from "vitest";
import { compareByThreadDampenedRank, priorityScore } from "./insights";
import type { AggregateForScoring, RankableAggregate } from "./insights";

describe("priorityScore -- Milestone 3 / A3 weighted_count usage", () => {
  it("uses weighted_count for frequency dominance when present, not raw count", () => {
    const dampened: AggregateForScoring = { count: 9, weighted_count: 3 };
    const undampened: AggregateForScoring = { count: 3, weighted_count: 3 };
    // Both have weighted_count=3 -- must score identically on the frequency
    // term despite very different raw counts.
    const scoreDampened = priorityScore(dampened, 3);
    const scoreUndampened = priorityScore(undampened, 3);
    expect(scoreDampened).toBe(scoreUndampened);
  });

  it("falls back to raw count when weighted_count is absent (older payload)", () => {
    const noWeighted: AggregateForScoring = { count: 9 };
    const withWeighted: AggregateForScoring = { count: 9, weighted_count: 9 };
    expect(priorityScore(noWeighted, 9)).toBe(priorityScore(withWeighted, 9));
  });

  it("still weights toward the group with the higher weighted_count", () => {
    const high: AggregateForScoring = { count: 3, weighted_count: 4 };
    const low: AggregateForScoring = { count: 9, weighted_count: 3 };
    expect(priorityScore(high, 4)).toBeGreaterThan(priorityScore(low, 4));
  });
});

describe("compareByThreadDampenedRank", () => {
  it("orders by weighted_count descending first", () => {
    const a: RankableAggregate = { aspect: "a", count: 1, weighted_count: 5 };
    const b: RankableAggregate = { aspect: "b", count: 1, weighted_count: 3 };
    expect(compareByThreadDampenedRank(a, b)).toBeLessThan(0); // a sorts first
  });

  it("breaks a weighted_count tie using thread_count descending -- not raw count", () => {
    // The exact contradiction caught during Milestone 3 plan review: the
    // single-thread group has the higher raw count but must still lose.
    const diverse: RankableAggregate = { aspect: "diverse", count: 3, weighted_count: 3, thread_count: 3 };
    const single: RankableAggregate = { aspect: "single", count: 9, weighted_count: 3, thread_count: 1 };
    expect(compareByThreadDampenedRank(diverse, single)).toBeLessThan(0); // diverse sorts first
  });

  it("falls back to raw count when weighted_count and thread_count both tie", () => {
    const big: RankableAggregate = { aspect: "big", count: 11, weighted_count: 4, thread_count: 2 };
    const small: RankableAggregate = { aspect: "small", count: 4, weighted_count: 4, thread_count: 2 };
    expect(compareByThreadDampenedRank(big, small)).toBeLessThan(0); // big sorts first
  });

  it("falls back to label ascending when everything else ties", () => {
    const zzz: RankableAggregate = { aspect: "zzz issue", count: 3, weighted_count: 3, thread_count: 1 };
    const aaa: RankableAggregate = { aspect: "aaa issue", count: 3, weighted_count: 3, thread_count: 1 };
    expect(compareByThreadDampenedRank(aaa, zzz)).toBeLessThan(0); // aaa sorts first
  });

  it("treats a missing weighted_count/thread_count as falling back to raw count / 1 thread", () => {
    const withFields: RankableAggregate = { aspect: "same", count: 5, weighted_count: 5, thread_count: 1 };
    const withoutFields: RankableAggregate = { aspect: "same", count: 5 };
    expect(compareByThreadDampenedRank(withFields, withoutFields)).toBe(0); // fully equivalent once defaults apply
  });

  it("reproduces the real run_55025c50e81b ordering end to end", () => {
    const groups: RankableAggregate[] = [
      { aspect: "floor damage", count: 11, weighted_count: 4, thread_count: 2 },
      { aspect: "app real time position update", count: 9, weighted_count: 3, thread_count: 1 },
      { aspect: "navigation/collision avoidance", count: 3, weighted_count: 3, thread_count: 3 },
      { aspect: "mop performance", count: 3, weighted_count: 3, thread_count: 3 },
      { aspect: "price vs value", count: 3, weighted_count: 3, thread_count: 3 },
      { aspect: "durability", count: 3, weighted_count: 3, thread_count: 2 },
      { aspect: "reliability", count: 3, weighted_count: 3, thread_count: 2 },
      { aspect: "security/privacy", count: 2, weighted_count: 2, thread_count: 1 },
    ];
    const sorted = [...groups].sort(compareByThreadDampenedRank);
    expect(sorted.map((g) => g.aspect)).toEqual([
      "floor damage",
      "mop performance",
      "navigation/collision avoidance",
      "price vs value",
      "durability",
      "reliability",
      "app real time position update",
      "security/privacy",
    ]);
  });
});
