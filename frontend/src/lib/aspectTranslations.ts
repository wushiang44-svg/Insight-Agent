import type { Language } from "./i18n";

// Aspect labels (e.g. "battery", "ease_of_use") are LLM free-text output, not a
// fixed enum (see app/react_agent.py's analyze_item prompt, which gives the LLM
// examples rather than a closed list) — a dictionary can never cover every value
// that shows up. This covers the common product-review vocabulary actually seen
// across real runs; anything not listed here falls back to the English original
// (title-cased) rather than showing a blank or a raw untranslated key.
const ZH_ASPECT_TRANSLATIONS: Record<string, string> = {
  battery: "电池",
  "battery life": "电池续航",
  comfort: "舒适度",
  quality: "质量",
  "quality_and_value": "质量与性价比",
  "quality and value": "质量与性价比",
  durability: "耐用性",
  price: "价格",
  value: "性价比",
  "value for money": "性价比",
  shipping: "物流",
  packaging: "包装",
  "customer_service": "客服",
  "customer service": "客服",
  "ease_of_use": "易用性",
  "ease of use": "易用性",
  noise: "噪音",
  "noise cancellation": "降噪",
  "noise_cancellation": "降噪",
  "sound quality": "音质",
  "sound_quality": "音质",
  "microphone quality": "麦克风质量",
  "microphone_quality": "麦克风质量",
  "build quality": "做工质量",
  "build_quality": "做工质量",
  grip: "抓地力",
  "non-slip": "防滑性",
  "non_slip": "防滑性",
  "non slip": "防滑性",
  size: "尺寸",
  width: "宽度",
  thickness: "厚度",
  fit: "合身度",
  cushioning: "缓冲性",
  smell: "气味",
  odor: "异味",
  "smell/odor": "气味/异味",
  "smell_odor": "气味/异味",
  staining: "染色问题",
  accessories: "配件",
  connectivity: "连接性",
  charging: "充电",
  "app experience": "App 体验",
  "app_experience": "App 体验",
  "water resistance": "防水性",
  "water_resistance": "防水性",
  controls: "操作按键",
  "find my feature": "查找功能",
  "find_my_feature": "查找功能",
  general: "综合",
};

export function translateAspect(aspect: string, language: Language): string {
  if (language === "en") return titleCase(aspect);
  const key = aspect.trim().toLowerCase();
  const translated = ZH_ASPECT_TRANSLATIONS[key];
  return translated ?? titleCase(aspect);
}

function titleCase(text: string): string {
  return text.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
