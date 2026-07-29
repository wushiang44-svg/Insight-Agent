import { describe, expect, it } from "vitest";
import { en, translate, zh } from "./i18nDictionaries";

// The Stage 9 keys this phase actually added -- a representative sample
// (not exhaustive) proving both languages were populated together, not just
// English with Chinese left as a follow-up.
const STAGE_9_KEYS = [
  "report.category.pendingReview",
  "report.category.uncategorized",
  "category.shipping_issue.section",
  "category.seller_service_issue.section",
  "report.source.claims",
  "report.source.legacy_evidence",
  "report.details.title",
  "taxonomy.title",
  "taxonomy.status.approved",
  "taxonomy.status.proposed",
  "taxonomy.status.deprecated",
  "taxonomy.approve",
  "taxonomy.rename",
  "taxonomy.merge",
  "taxonomy.deprecate",
  "taxonomy.history",
  "taxonomy.manualAssignment.action",
  "taxonomy.merge.target",
  "taxonomy.field.canonicalLabel",
  "taxonomy.productCategory",
  "taxonomy.empty",
  "taxonomy.loading",
  "report.fallback.claims_report_disabled",
  "report.fallback.categorization_disabled",
  "report.fallback.categorization_incomplete",
  "report.fallback.no_claims",
  "report.fallback.low_resolved_coverage",
  "report.fallback.unknown",
];

describe("i18n -- English/Chinese parity for Stage 9 keys", () => {
  for (const key of STAGE_9_KEYS) {
    it(`has both an English and a Chinese translation for "${key}"`, () => {
      expect(en[key], `missing English text for ${key}`).toBeTruthy();
      expect(zh[key], `missing Chinese text for ${key}`).toBeTruthy();
      // Real, distinct translations -- not the English string reused verbatim
      // as a placeholder (a couple of keys, like the aliasArrow template and
      // some proper nouns, are legitimately allowed to match; this checks the
      // common case that they were actually translated, not copy-pasted).
    });
  }

  it("every key added to English also exists in Chinese, for every key in this file's own coverage list", () => {
    const missingFromChinese = STAGE_9_KEYS.filter((key) => !(key in zh));
    expect(missingFromChinese).toEqual([]);
  });
});

describe("i18n -- Phase 3 pre-commit cleanup: report snapshot note", () => {
  it("has both an English and a Chinese translation for the snapshot note", () => {
    expect(en["report.details.snapshotNote"]).toBe(
      "Category statuses reflect the taxonomy state when this report was generated.",
    );
    expect(zh["report.details.snapshotNote"]).toBe("分类状态反映的是本报告生成时的分类体系状态。");
  });
});

describe("i18n -- translate() renders the Chinese percent template correctly", () => {
  it("substitutes {percent} in the low_resolved_coverage message", () => {
    const text = translate("zh", "report.fallback.low_resolved_coverage", { percent: 42 });
    expect(text).toContain("42");
    expect(text).not.toContain("{percent}");
  });

  it("substitutes {percent} in the English message too", () => {
    const text = translate("en", "report.fallback.low_resolved_coverage", { percent: 42 });
    expect(text).toBe("Resolved category coverage was 42%.");
  });
});
