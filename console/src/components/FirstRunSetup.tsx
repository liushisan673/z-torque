import { lazy, Suspense, useEffect, useState } from "react";
import { request } from "../services/api";
import { useAuth } from "../stores/auth";
import { isAdminUser } from "./AppTopbar";
import { Modal } from "./Modal";

const OnboardingForm = lazy(() =>
  import("../pages/Onboarding").then((module) => ({ default: module.OnboardingForm })),
);

interface OnboardingStatus {
  configured: boolean;
  needed: boolean;
}

/** 管理员首次登录时强制完成模型密钥配置；配置完成后弹窗自动消失。 */
export function FirstRunSetup() {
  const { user, initializing } = useAuth();
  const [checked, setChecked] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (initializing || !user) return;
    if (!isAdminUser(user)) {
      setChecked(true);
      return;
    }
    let cancelled = false;
    void request<OnboardingStatus>("/api/z-torque/onboarding/status")
      .then((status) => {
        if (cancelled) return;
        setOpen(status.needed);
        setChecked(true);
      })
      .catch(() => {
        if (!cancelled) setChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [user, initializing]);

  if (!checked || !open || !isAdminUser(user)) return null;

  return (
    <Modal
      open
      title="首次使用：配置模型密钥"
      onClose={() => setOpen(false)}
      closeOnBackdrop={false}
      width={660}
    >
      <p className="onboarding-lead">
        智能体、技能、知识库与模型标识均已内置并绑定。只需填写一次 API Key，即可正常对话；请勿修改模型标识。
      </p>
      <Suspense fallback={null}>
        <OnboardingForm compact onDone={() => setOpen(false)} />
      </Suspense>
    </Modal>
  );
}
