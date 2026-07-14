export type Category = "pain_point" | "feature_request" | "praise" | "comparison";

export interface CategoryStyle {
  color: string;
}

// Fixed categorical order — validated for CVD separation in both light and dark
// steps (see palette validation notes). Never cycled, never reassigned per view.
// Text (label/sectionTitle/emptyText) lives in i18n.ts under `category.<name>.*`
// so it can be translated — look it up with `t()` rather than reading it here.
export const CATEGORY_STYLES: Record<Category, CategoryStyle> = {
  pain_point: { color: "var(--cat-pain)" },
  feature_request: { color: "var(--cat-feature)" },
  praise: { color: "var(--cat-praise)" },
  comparison: { color: "var(--cat-competitor)" },
};

export const SENTIMENT_COLORS: Record<string, string> = {
  positive: "var(--sent-positive)",
  neutral: "var(--sent-neutral)",
  negative: "var(--sent-negative)",
};
