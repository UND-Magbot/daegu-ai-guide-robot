"use client";

import React, { useEffect } from "react";
import { useRouter } from "next/navigation";
import Header from "@/app/components/common/Header";
import Sidebar from "@/app/components/common/Sidebar";
import GlobalErrorAlert from "@/app/components/common/GlobalErrorAlert";
import GlobalLoading from "@/app/components/common/GlobalLoading";
import ThermalAlertWatcher from "@/app/components/common/ThermalAlertWatcher";
import InitPoseAlertWatcher from "@/app/components/common/InitPoseAlertWatcher";
import GuideFollowAlertWatcher from "@/app/components/common/GuideFollowAlertWatcher";
import BatteryResumeAlertWatcher from "@/app/components/common/BatteryResumeAlertWatcher";
import GuideBatteryAbortAlertWatcher from "@/app/components/common/GuideBatteryAbortAlertWatcher";
import StandbyMapHoldAlertWatcher from "@/app/components/common/StandbyMapHoldAlertWatcher";
import NavHoldAlertWatcher from "@/app/components/common/NavHoldAlertWatcher";
import GuideTtsFailedAlertWatcher from "@/app/components/common/GuideTtsFailedAlertWatcher";
import ServerHealthBanner from "@/app/components/common/ServerHealthBanner";
import { PageLoadingProvider } from "@/app/context/PageLoadingContext";
import { ToastProvider } from "@/app/components/common/Toast";
import { SidebarProvider } from "@/app/context/SidebarContext";
import { AlertProvider } from "@/app/context/AlertContext";
import { RobotStatusProvider } from "@/app/context/RobotStatusContext";
import { useAuth } from "@/app/context/AuthContext";

export default function PagesLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { isAuthenticated, isLoading, isManualLogout } = useAuth();
  // 미인증 시 로그인 페이지로 (로그아웃/세션 만료 구분)
  const wasAuthenticated = React.useRef(false);
  useEffect(() => {
    if (isAuthenticated) {
      wasAuthenticated.current = true;
    }
    if (!isLoading && !isAuthenticated) {
      if (wasAuthenticated.current && !isManualLogout.current) {
        router.replace("/login?reason=session_expired");
      } else {
        router.replace("/login");
      }
    }
  }, [isLoading, isAuthenticated, isManualLogout, router]);

  // 로딩 중 또는 미인증
  if (isLoading || !isAuthenticated) return null;

  return (
    <SidebarProvider>
      <RobotStatusProvider>
      <AlertProvider>
        <ToastProvider>
          <PageLoadingProvider>
          <GlobalLoading />
          <ServerHealthBanner />
          <Header />
          <Sidebar />
          <main className="page-container">{children}</main>

          <GlobalErrorAlert />
          <ThermalAlertWatcher />
          <InitPoseAlertWatcher />
          <GuideFollowAlertWatcher />
          <BatteryResumeAlertWatcher />
          {/* 이어 가기 확인창 뒤에 둬 위에 뜨게 한다 — 무슨 일이 있었는지 먼저 읽고 정한다 */}
          <GuideBatteryAbortAlertWatcher />
          <StandbyMapHoldAlertWatcher />
          <NavHoldAlertWatcher />
          <GuideTtsFailedAlertWatcher />
          </PageLoadingProvider>
        </ToastProvider>
      </AlertProvider>
      </RobotStatusProvider>
    </SidebarProvider>
  );
}
