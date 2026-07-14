import type { AppConfig, DataSource } from "../api";
import { useLanguage } from "./i18n";

export interface SourceMeta {
  key: DataSource;
  label: string;
  /** Plural noun for the grouping dimension: subreddits / products / videos / sources. */
  groupLabel: string;
  groupLabelSingular: string;
  /** What one piece of evidence is called: comments / reviews / items. */
  itemNoun: string;
  itemNounSingular: string;
  /** Prefixed onto a quote's citation, e.g. "r/" for Reddit; empty for sources with no such convention. */
  citationPrefix: string;
  /** Which AppConfig flag gates this source's availability; undefined means always available. */
  configKey?: keyof AppConfig;
}

interface SourceStructure {
  groupKey: "subreddit" | "product" | "video" | "source";
  itemKey: "comment" | "review" | "item";
  citationPrefix: string;
  configKey?: keyof AppConfig;
}

// Language-independent structure only — the actual label text is translated via
// i18n.ts, keyed off `groupKey`/`itemKey`/the DataSource itself. Keeping this
// separate from the translations means adding a language never means touching
// this file, and adding a data source never means touching i18n.ts's shape.
const STRUCTURE: Record<DataSource, SourceStructure> = {
  reddit_api: { groupKey: "subreddit", itemKey: "comment", citationPrefix: "r/", configKey: "reddit_configured" },
  reddit_scraper: { groupKey: "subreddit", itemKey: "comment", citationPrefix: "r/" },
  json_upload: { groupKey: "source", itemKey: "item", citationPrefix: "" },
  amazon: { groupKey: "product", itemKey: "review", citationPrefix: "", configKey: "amazon_configured" },
  youtube: { groupKey: "video", itemKey: "comment", citationPrefix: "", configKey: "youtube_configured" },
};

/** Falls back to the Reddit shape for a run whose data_source predates this source (or hasn't loaded yet). */
export function useSourceMeta(dataSource: DataSource | undefined): SourceMeta {
  const { t } = useLanguage();
  const key: DataSource = dataSource && STRUCTURE[dataSource] ? dataSource : "reddit_api";
  const struct = STRUCTURE[key];
  return {
    key,
    label: t(`source.${key}.label`),
    groupLabel: t(`source.group.${struct.groupKey}`),
    groupLabelSingular: t(`source.group.${struct.groupKey}.singular`),
    itemNoun: t(`source.item.${struct.itemKey}s`),
    itemNounSingular: t(`source.item.${struct.itemKey}`),
    citationPrefix: struct.citationPrefix,
    configKey: struct.configKey,
  };
}
