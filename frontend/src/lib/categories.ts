export type Category = "pain_point" | "feature_request" | "praise" | "comparison" | "shipping_issue" | "seller_service_issue";

export interface CategoryStyle {
  color: string;
}

// Fixed categorical order — validated for CVD separation in both light and dark
// steps (see palette validation notes). Never cycled, never reassigned per view.
// Text (label/sectionTitle/emptyText) lives in i18n.ts under `category.<name>.*`
// so it can be translated — look it up with `t()` rather than reading it here.
// shipping_issue/seller_service_issue (Phase 3, Stage 9) reuse the same
// pattern but were not put through the full CVD-palette validation pass the
// original four were -- see index.css's --cat-shipping/--cat-service comment.
export const CATEGORY_STYLES: Record<Category, CategoryStyle> = {
  pain_point: { color: "var(--cat-pain)" },
  feature_request: { color: "var(--cat-feature)" },
  praise: { color: "var(--cat-praise)" },
  comparison: { color: "var(--cat-competitor)" },
  shipping_issue: { color: "var(--cat-shipping)" },
  seller_service_issue: { color: "var(--cat-service)" },
};

export const SENTIMENT_COLORS: Record<string, string> = {
  positive: "var(--sent-positive)",
  neutral: "var(--sent-neutral)",
  negative: "var(--sent-negative)",
};
