/**
 * 偏好设置页：外观偏好存本机 localStorage；文件直出等运行时开关存本机数据目录并即时生效。
 * 通用组件文案走轻量字典，缺失回退中文。
 */
import { useEffect, useState } from "react";
import { Card, Segmented, Switch, Typography } from "antd";
import type { Locale } from "../i18n/messages";
import { request } from "../services/api";
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
  const [fileAutoKeep, setFileAutoKeep] = useState(false);
  const [fileSettingReady, setFileSettingReady] = useState(false);
  const [fileSettingBusy, setFileSettingBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void request<{ file_change_auto_keep: boolean }>("/api/z-torque/settings/runtime")
      .then((data) => {
        if (!cancelled) {
          setFileAutoKeep(Boolean(data.file_change_auto_keep));
          setFileSettingReady(true);
        }
      })
      .catch(() => {
        if (!cancelled) setFileSettingReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggleFileAutoKeep(value: boolean) {
    setFileSettingBusy(true);
    try {
      const data = await request<{ file_change_auto_keep: boolean }>(
        "/api/z-torque/settings/runtime/file-change-auto-keep",
        { method: "PUT", body: { auto_keep: value } },
      );
      setFileAutoKeep(Boolean(data.file_change_auto_keep));
    } finally {
      setFileSettingBusy(false);
    }
  }

  return (
    <div className="settings-page">
      <div className="settings-head">
        <Title level={3}>{t("settings.title")}</Title>
        <Paragraph type="secondary">{t("settings.subtitle")}</Paragraph>
      </div>

      <Card className="settings-card" title="文件输出">
        <div className="settings-row">
          <div>
            <div>文件直出</div>
            <Paragraph type="secondary" className="settings-hint">
              开启后，生成文档将直接保存为会话附件；关闭时先生成变更提案，需在评审面板采纳后才会保存。
            </Paragraph>
          </div>
          <Switch
            checked={fileAutoKeep}
            disabled={!fileSettingReady || fileSettingBusy}
            loading={fileSettingBusy}
            onChange={(value) => void toggleFileAutoKeep(value)}
          />
        </div>
      </Card>

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
