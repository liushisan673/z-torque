/**
 * 偏好设置页（Phase 2 §四 i18n / 主题 / 背景 / 字号进阶）。
 *
 * 全部偏好只存本机 localStorage，经 ThemeProvider 落到 <html data-*>，
 * 不写后端、不影响其他用户；通用组件文案走轻量字典，缺失回退中文。
 */
import { Card, Segmented, Typography } from "antd";
import type { Locale } from "../i18n/messages";
import { usePreferences } from "../theme/ThemeProvider";
import type { BackgroundChoice, FontScale } from "../theme/ThemeProvider";

const { Title, Paragraph, Text } = Typography;

export function SettingsPage() {
  const {
    mode,
    setMode,
    locale,
    setLocale,
    background,
    setBackground,
    fontScale,
    setFontScale,
    t,
  } = usePreferences();

  return (
    <div className="settings-page">
      <div className="settings-head">
        <Title level={3}>{t("settings.title")}</Title>
        <Paragraph type="secondary">{t("settings.subtitle")}</Paragraph>
      </div>

      <Card className="settings-card" title={t("settings.section.language")}>
        <Segmented
          block
          value={locale}
          options={[
            { label: t("settings.language.zh"), value: "zh" },
            { label: t("settings.language.en"), value: "en" },
          ]}
          onChange={(value) => setLocale(value as Locale)}
        />
        <Paragraph type="secondary" className="settings-hint">{t("settings.language.hint")}</Paragraph>
      </Card>

      <Card className="settings-card" title={t("settings.section.appearance")}>
        <div className="settings-row">
          <Text>{t("settings.appearance.mode")}</Text>
          <Segmented
            value={mode}
            options={[
              { label: t("settings.appearance.light"), value: "light" },
              { label: t("settings.appearance.dark"), value: "dark" },
            ]}
            onChange={(value) => setMode(value as "light" | "dark")}
          />
        </div>
        <div className="settings-row">
          <Text>{t("settings.appearance.background")}</Text>
          <Segmented
            value={background}
            options={[
              { label: t("settings.background.default"), value: "default" },
              { label: t("settings.background.soft"), value: "soft" },
              { label: t("settings.background.warm"), value: "warm" },
            ]}
            onChange={(value) => setBackground(value as BackgroundChoice)}
          />
        </div>
      </Card>

      <Card className="settings-card" title={t("settings.section.reading")}>
        <div className="settings-row">
          <Text>{t("settings.reading.fontScale")}</Text>
          <Segmented
            value={fontScale}
            options={[
              { label: t("settings.fontScale.compact"), value: "compact" },
              { label: t("settings.fontScale.normal"), value: "normal" },
              { label: t("settings.fontScale.comfortable"), value: "comfortable" },
            ]}
            onChange={(value) => setFontScale(value as FontScale)}
          />
        </div>
        <Paragraph type="secondary">{t("settings.preview")}</Paragraph>
        <div className="settings-preview">{t("settings.preview.text")}</div>
      </Card>
    </div>
  );
}

export default SettingsPage;
