"use client";

import { useRef, useEffect, useState, useCallback, forwardRef, useImperativeHandle } from "react";
import {
  WebGLRenderer, Scene, PerspectiveCamera,
  AmbientLight, HemisphereLight, DirectionalLight,
  Mesh, InstancedMesh, Group, Object3D,
  PlaneGeometry, BoxGeometry, CylinderGeometry, SphereGeometry, RingGeometry, ConeGeometry, TubeGeometry, ExtrudeGeometry, Shape, Path, Vector2,
  MeshStandardMaterial,
  CanvasTexture, Color, Vector3,
  FogExp2, PCFShadowMap, ACESFilmicToneMapping, SRGBColorSpace,
  LinearFilter, DoubleSide,
  CatmullRomCurve3, LineCurve3, Sprite, SpriteMaterial,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { CanvasMapProps } from "@/app/components/map/types";
import type { CanvasMapHandle } from "@/app/components/map/CanvasMap";
import type { ZoomAction } from "@/app/utils/zoom";
import { loadImage, processMapImage3D } from "@/app/utils/mapImageProcessor";
import { worldTo3D, getMapDimensions } from "./mapCoordinates3D";
import { createRobotModel } from "./robotModel3D";

/* ── SceneCtx ── */
type SceneCtx = {
  renderer: WebGLRenderer;
  scene: Scene;
  camera: PerspectiveCamera;
  controls: OrbitControls;
  robot: Group | null;
  robotLabel: HTMLDivElement | null;
  poiLabels: HTMLDivElement[];
  floorMesh: Mesh | null;
  pathGroup: Group | null;
  nodeGroup: Group | null;     // 노드망·이동 경로 (2026-10-19)
  needsRender: boolean;
  // 맵 이미지 로드가 끝나기 전에는 렌더를 하지 않는다 — 바닥 없는
  // POI/경로만 잠깐 비쳐 "잔상"처럼 보이는 것을 방지
  floorReady: boolean;

  animId: number;
};

type Map3DCanvasExtraProps = {
  /** 추적할 POI — 지정 시 이 POI의 스크린 좌표를 onTrackedPoiScreen으로 계속 알림 */
  trackedPoi?: { x: number; y: number } | null;
  onTrackedPoiScreen?: (screen: { x: number; y: number; behind: boolean } | null) => void;
};

// 이동 구간 튜브 — 10-22 사용자 요청으로 끔(2D 와 같이 노드망 바닥 화살표만)
const SHOW_ROUTE_TUBE = false;
// 지날 노드 번호 배지 — 10-29 사용자 요청으로 끔(번호가 한 바퀴 전체 순번이라 25·26 처럼 보여 헷갈림)
const SHOW_ROUTE_NODE_BADGES = false;

const Map3DCanvas = forwardRef<CanvasMapHandle, CanvasMapProps & Map3DCanvasExtraProps>(function Map3DCanvas(
  {
    config, robotPos, robotName, pois, navPath, selectedPoiId,
    navNetwork = null, networkLevel = "faint", activeRoute = null,
    showRobot = false, showPois = false, showPath = false, showLabels = true,
    onPoiClick, trackedPoi, onTrackedPoiScreen, className, style,
  },
  ref
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<SceneCtx | null>(null);
  const [, setIsLoading] = useState(true);

  const worldToPixelScreen = useCallback(
    (wx: number, wy: number) => { const p = worldTo3D(wx, wy, config); return { x: p.x, y: p.z }; },
    [config]
  );
  const pixelToWorldScreen = useCallback(
    (px: number, py: number) => {
      const mw = config.pixelWidth * config.resolution;
      const mh = config.pixelHeight * config.resolution;
      return { x: px + config.originX + mw / 2, y: -(py - config.originY - mh / 2) };
    },
    [config]
  );
  const handleZoom = useCallback((action: ZoomAction) => {
    const ctx = sceneRef.current;
    if (!ctx) return;
    const { controls } = ctx;
    if (action === "reset") {
      const { width: mapW, height: mapH } = getMapDimensions(config);
      const dist = Math.max(mapW, mapH) * 0.9;
      controls.target.set(0, 0, 0);
      controls.object.position.set(dist * 0.15, dist * 0.75, dist * 0.55);
      controls.update();
      ctx.needsRender = true;
    }
  }, [config]);
  const projectToScreen = useCallback((wx: number, wy: number) => {
    const ctx = sceneRef.current;
    const container = containerRef.current;
    if (!ctx || !container) return { x: 0, y: 0, behind: true };
    const p = worldTo3D(wx, wy, config);
    const v = new Vector3(p.x, 0.85, p.z).project(ctx.camera);
    return {
      x: (v.x * 0.5 + 0.5) * container.clientWidth,
      y: (-v.y * 0.5 + 0.5) * container.clientHeight,
      behind: v.z > 1,
    };
  }, [config]);

  useImperativeHandle(ref, () => ({ handleZoom, worldToPixelScreen, pixelToWorldScreen, projectToScreen }));

  /* ═══ 추적 POI 스크린 좌표 업데이트 (클릭 카드 위치용) ═══ */
  useEffect(() => {
    if (!trackedPoi || !onTrackedPoiScreen) return;
    const ctx = sceneRef.current;
    const container = containerRef.current;
    if (!ctx || !container) return;

    const emit = () => {
      const p = worldTo3D(trackedPoi.x, trackedPoi.y, config);
      const v = new Vector3(p.x, 0.85, p.z).project(ctx.camera);
      onTrackedPoiScreen({
        x: (v.x * 0.5 + 0.5) * container.clientWidth,
        y: (-v.y * 0.5 + 0.5) * container.clientHeight,
        behind: v.z > 1,
      });
    };

    emit();
    ctx.controls.addEventListener("change", emit);
    return () => {
      ctx.controls.removeEventListener("change", emit);
    };
  }, [trackedPoi, onTrackedPoiScreen, config]);

  /* ═══ Scene 초기화 ═══ */
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Renderer — dpr 제한, 섀도우맵 1024
    const renderer = new WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    renderer.setSize(container.clientWidth, container.clientHeight);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = PCFShadowMap;
    renderer.toneMapping = ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.1;
    renderer.outputColorSpace = SRGBColorSpace;
    container.appendChild(renderer.domElement);

    const scene = new Scene();
    scene.background = new Color("#12151f");
    scene.fog = new FogExp2("#12151f", 0.015);

    const { width: mapW, height: mapH } = getMapDimensions(config);
    const dist = Math.max(mapW, mapH) * 0.9;

    const camera = new PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 300);
    camera.position.set(dist * 0.15, dist * 0.75, dist * 0.55);
    camera.lookAt(0, 0, 0);

    // 조명 — 키/필 2개 + ambient/hemi (림 라이트 제거)
    scene.add(new AmbientLight("#6688bb", 0.5));
    scene.add(new HemisphereLight("#8899cc", "#223344", 0.4));

    const keyLight = new DirectionalLight("#ffffff", 0.9);
    keyLight.position.set(15, 25, 12);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.set(1024, 1024);
    keyLight.shadow.camera.near = 1;
    keyLight.shadow.camera.far = 80;
    keyLight.shadow.camera.left = -25;
    keyLight.shadow.camera.right = 25;
    keyLight.shadow.camera.top = 25;
    keyLight.shadow.camera.bottom = -25;
    keyLight.shadow.bias = -0.002;
    scene.add(keyLight);

    const fillLight = new DirectionalLight("#88aaff", 0.3);
    fillLight.position.set(-8, 12, -8);
    scene.add(fillLight);

    // 아래쪽 바닥 (경계 바깥 영역)
    const basePlane = new Mesh(
      new PlaneGeometry(mapW * 2, mapH * 2),
      new MeshStandardMaterial({ color: "#0e1018", roughness: 0.95 })
    );
    basePlane.rotation.x = -Math.PI / 2;
    basePlane.position.y = 0;
    basePlane.receiveShadow = true;
    scene.add(basePlane);

    // Controls — change 이벤트로 dirty flag
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minPolarAngle = Math.PI / 12;
    controls.maxPolarAngle = Math.PI / 2.1;
    controls.minDistance = 3;
    controls.maxDistance = 100;
    controls.target.set(0, 0, 0);
    controls.zoomSpeed = 0.8;
    controls.rotateSpeed = 0.6;

    // ctx를 먼저 만들고 모든 render 트리거는 ctx.needsRender 하나로 통일.
    // (이전에는 로컬 `let needsRender`와 `ctx.needsRender` 두 개가 따로 살아
    //  navPath/robot/poi 등 외부 effect가 ctx.needsRender를 true로 바꿔도
    //  animate loop는 로컬 변수만 보고 있어 스크린 갱신이 안 되는 버그 존재)
    const ctx: SceneCtx = {
      renderer, scene, camera, controls,
      robot: null, robotLabel: null, poiLabels: [],
      floorMesh: null, pathGroup: null, nodeGroup: null,
      needsRender: false,          // floorReady 되기 전까지 렌더 금지
      floorReady: false,
      animId: 0,
    };
    sceneRef.current = ctx;

    controls.addEventListener("change", () => { ctx.needsRender = true; });

    // on-demand render loop (idle 시 0fps).
    // floorReady 되기 전까지는 렌더하지 않아 바닥 없는 POI/경로가 잠깐 비치는
    // 현상을 차단.
    const animate = () => {
      ctx.animId = requestAnimationFrame(animate);
      controls.update();
      if (ctx.needsRender && ctx.floorReady) {
        renderer.render(scene, camera);
        ctx.needsRender = false;
      }
    };
    animate();

    const ro = new ResizeObserver(() => {
      const w = container.clientWidth;
      const h = container.clientHeight;
      if (w > 0 && h > 0) {
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
        ctx.needsRender = true;
      }
    });
    ro.observe(container);

    // 맵 로딩
    setIsLoading(true);
    loadImage(config.imageSrc)
      .then((img) => {
        if (!sceneRef.current) return;
        const processed = processMapImage3D(img, config.imageSrc);
        const { width: mw, height: mh } = getMapDimensions(config);

        const floorTex = new CanvasTexture(processed);
        floorTex.minFilter = LinearFilter;
        floorTex.magFilter = LinearFilter;
        floorTex.colorSpace = SRGBColorSpace;

        const floorMesh = new Mesh(
          new PlaneGeometry(mw, mh),
          new MeshStandardMaterial({ map: floorTex, transparent: true, roughness: 0.85, side: DoubleSide })
        );
        floorMesh.rotation.x = -Math.PI / 2;
        floorMesh.position.y = 0.15; // 맵 내부를 높여서 3D 느낌
        floorMesh.receiveShadow = true;
        scene.add(floorMesh);
        ctx.floorMesh = floorMesh;

        // 맵 외곽 경계 벽
        const borderMap = extractBorderFromImage(processed);
        const wallMesh = buildBorderWalls(borderMap, mw, mh);
        if (wallMesh) scene.add(wallMesh);

        // 바닥이 준비되어야 POI/경로/로봇이 "공중에 떠있는" 상태로 먼저
        // 그려지지 않는다. 여기서 처음으로 렌더 허가를 낸다.
        ctx.floorReady = true;
        ctx.needsRender = true;
        setIsLoading(false);
      })
      .catch(() => setIsLoading(false));

    return () => {
      cancelAnimationFrame(ctx.animId);
      ro.disconnect();

      // 전체 씬 리소스 dispose (M-4): scene.remove만으로는 GPU 버퍼/텍스처가
      // 회수되지 않는다. floorMesh/basePlane/wall/floorTex(CanvasTexture) 등
      // 모든 mesh의 geometry·material·map을 명시적으로 해제해 층 전환 반복 시
      // VRAM 누적을 방지한다. (아래 pathGroup/POI 개별 dispose는 멱등이라 무해)
      const disposeMaterial = (m: MeshStandardMaterial | null | undefined) => {
        if (!m) return;
        m.map?.dispose();
        m.emissiveMap?.dispose();
        m.dispose();
      };
      try {
        scene.traverse((obj) => {
          const mesh = obj as Mesh;
          if (mesh.isMesh) {
            mesh.geometry?.dispose();
            const mat = mesh.material;
            if (Array.isArray(mat)) mat.forEach((m) => disposeMaterial(m as MeshStandardMaterial));
            else disposeMaterial(mat as MeshStandardMaterial);
          }
        });
      } catch {}

      // 씬을 완전히 비우고 한 번 더 렌더 → 캔버스 버퍼가 빈 프레임으로 덮여씀.
      // React가 canvas를 DOM에서 떼어내는 타이밍이 한두 프레임 늦더라도
      // 잔상이 보이지 않도록 물리적으로 비움.
      try {
        while (scene.children.length > 0) scene.remove(scene.children[0]);
        renderer.setClearColor(new Color("#12151f"), 1);
        renderer.clear(true, true, true);
        renderer.render(scene, camera);
      } catch {}
      controls.dispose();
      if (sceneRef.current) {
        // pathGroup dispose
        const pg = sceneRef.current.pathGroup;
        if (pg) {
          pg.traverse((obj) => {
            const mesh = obj as Mesh;
            if (mesh.isMesh) {
              mesh.geometry?.dispose();
              const mat = mesh.material;
              if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
              else mat?.dispose();
            }
          });
          sceneRef.current.scene.remove(pg);
          sceneRef.current.pathGroup = null;
        }
        // POI mesh dispose (scene.children에서 isPoi 플래그로 식별)
        const poiChildren = [...sceneRef.current.scene.children].filter((c) => c.userData.isPoi);
        poiChildren.forEach((c) => {
          const mesh = c as Mesh;
          if (mesh.isMesh) {
            mesh.geometry?.dispose();
            const mat = mesh.material;
            if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
            else mat?.dispose();
          }
          sceneRef.current!.scene.remove(c);
        });
        sceneRef.current.robotLabel?.remove();
        sceneRef.current.poiLabels.forEach((l) => l.remove());
        sceneRef.current.poiLabels = [];
      }
      renderer.dispose();
      renderer.domElement.parentNode?.removeChild(renderer.domElement);
      sceneRef.current = null;
    };
  }, [config]);

  /* ═══ Robot ═══ */
  useEffect(() => {
    const ctx = sceneRef.current;
    if (!ctx) return;

    if (ctx.robot) { ctx.scene.remove(ctx.robot); ctx.robot = null; }
    ctx.robotLabel?.remove();
    ctx.robotLabel = null;

    if (!showRobot || !robotPos) return;

    const p = worldTo3D(robotPos.x, robotPos.y, config);
    const robot = createRobotModel();
    robot.position.set(p.x, 0.15, p.z); // 맵 바닥(0.15) 위에 배치
    robot.rotation.y = -robotPos.yaw;

    // 진행 방향 화살촉 (크고 통통)
    const arrowShape = new Shape();
    arrowShape.moveTo(0.22, 0);        // 뾰족 끝
    arrowShape.lineTo(-0.08, -0.12);   // 좌하
    arrowShape.lineTo(0.0, 0);         // 안쪽 홈
    arrowShape.lineTo(-0.08, 0.12);    // 좌상
    arrowShape.closePath();

    const arrowGeo = new ExtrudeGeometry(arrowShape, {
      depth: 0.02, bevelEnabled: true, bevelThickness: 0.008, bevelSize: 0.006, bevelSegments: 2,
    });
    const arrowMat = new MeshStandardMaterial({
      color: "#1A73E8", emissive: new Color("#1A73E8"),
      emissiveIntensity: 0.6, roughness: 0.2, metalness: 0.3,
    });
    const arrow = new Mesh(arrowGeo, arrowMat);
    arrow.rotation.x = -Math.PI / 2; // XY평면 → XZ평면(바닥)
    arrow.position.set(0.7, 0.01, 0);
    robot.add(arrow);
    ctx.scene.add(robot);
    ctx.robot = robot;
    ctx.needsRender = true;

    if (robotName && containerRef.current) {
      const label = mkLabel(robotName, {
        color: "#4da6ff",
        fontSize: "12px", fontWeight: "700", fontFamily: "'Inter',sans-serif",
        textShadow: "0 1px 4px rgba(0,0,0,0.8)",
      });
      containerRef.current.appendChild(label);
      ctx.robotLabel = label;

      const update = () => {
        if (!ctx.robot || !ctx.robotLabel || !containerRef.current) return;
        project(new Vector3(p.x, 1.4, p.z), ctx.camera, containerRef.current, ctx.robotLabel, "translate(-50%,-100%)");
      };
      update();
      ctx.controls.addEventListener("change", update);
      return () => { ctx.controls.removeEventListener("change", update); };
    }
  }, [robotPos, showRobot, robotName, config]);

  /* ═══ POI ═══ */
  useEffect(() => {
    const ctx = sceneRef.current;
    if (!ctx) return;

    // 이전 POI 메시·재질·지오메트리를 확실히 dispose 하고 제거
    const clearPois = () => {
      ctx.poiLabels.forEach((l) => l.remove());
      ctx.poiLabels = [];
      const poiChildren = [...ctx.scene.children].filter((c) => c.userData.isPoi);
      poiChildren.forEach((c) => {
        const mesh = c as Mesh;
        if (mesh.isMesh) {
          mesh.geometry?.dispose();
          const mat = mesh.material;
          if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
          else mat?.dispose();
        }
        ctx.scene.remove(c);
      });
      ctx.needsRender = true;
    };

    clearPois();

    if (!showPois || !pois || pois.length === 0) return;
    const container = containerRef.current;
    if (!container) return;

    const COLORS: Record<string, string> = {
      work: "#e84040", charge: "#2e9e3e", standby: "#7c4dff", waypoint: "#1a8cff",
    };
    const updateFns: (() => void)[] = [];

    // 맵 핀 지오메트리 (납작한 판 + 구멍 — ExtrudeGeometry)
    const pinGeo = createPinGeo();
    const baseRingGeo = new RingGeometry(0.06, 0.15, 20);

    pois.forEach((poi) => {
      const pos = worldTo3D(poi.x, poi.y, config);
      const cat = poi.category || "work";
      const isSel = poi.id === selectedPoiId;
      const color = isSel ? "#64b4ff" : (COLORS[cat] ?? "#ff6b6b");

      const pinMat = new MeshStandardMaterial({
        color, emissive: new Color(color), emissiveIntensity: isSel ? 0.6 : 0.4,
        roughness: 0.1, metalness: 0.4, side: DoubleSide,
      });

      // 맵 핀 (납작한 판, 바닥 위에 서있음)
      const pin = new Mesh(pinGeo, pinMat);
      pin.position.set(pos.x, 0.15, pos.z);
      pin.userData.isPoi = true;
      ctx.scene.add(pin);

      // 핀 밑 바닥 링
      const baseRing = new Mesh(baseRingGeo, new MeshStandardMaterial({
        color, emissive: new Color(color),
        emissiveIntensity: 0.5, transparent: true, opacity: 0.6, side: DoubleSide,
      }));
      baseRing.rotation.x = -Math.PI / 2;
      baseRing.position.set(pos.x, 0.16, pos.z);
      baseRing.userData.isPoi = true;
      ctx.scene.add(baseRing);

      // HTML 라벨 (컴팩트)
      if (showLabels) {
        const label = mkLabel(poi.name, {
          color: "#fff", fontSize: "11px", fontWeight: "600", fontFamily: "'Inter',sans-serif",
          background: `${color}dd`, padding: "2px 8px", borderRadius: "4px",
          boxShadow: `0 2px 6px rgba(0,0,0,0.4)`,
          cursor: onPoiClick ? "pointer" : "default",
          pointerEvents: onPoiClick ? "auto" : "none",
        });
        if (onPoiClick) label.addEventListener("click", () => onPoiClick(poi));
        container.appendChild(label);
        ctx.poiLabels.push(label);

        const upd = () => project(new Vector3(pos.x, 0.85, pos.z), ctx.camera, container, label, "translate(-50%,-100%)");
        upd();
        updateFns.push(upd);
      }
    });

    ctx.needsRender = true;
    const onChange = () => updateFns.forEach((fn) => fn());
    ctx.controls.addEventListener("change", onChange);
    return () => {
      ctx.controls.removeEventListener("change", onChange);
      // effect 재실행/언마운트 시 POI 리소스 확실히 정리
      clearPois();
    };
  }, [pois, showPois, showLabels, selectedPoiId, config, onPoiClick]);

  /* ═══ NavPath ═══ */
  useEffect(() => {
    const ctx = sceneRef.current;
    if (!ctx) return;

    // 이전 pathGroup의 geometry/material을 명시적으로 dispose (GPU 리소스 회수)
    const disposePathGroup = (group: Group | null) => {
      if (!group) return;
      group.traverse((obj) => {
        const mesh = obj as Mesh;
        if (mesh.isMesh) {
          mesh.geometry?.dispose();
          const mat = mesh.material;
          if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
          else mat?.dispose();
        }
      });
    };

    if (ctx.pathGroup) {
      ctx.scene.remove(ctx.pathGroup);
      disposePathGroup(ctx.pathGroup);
      ctx.pathGroup = null;
      ctx.needsRender = true;
    }
    if (!showPath || !navPath || navPath.segments.length === 0) return;

    const pathGroup = new Group();

    // 2D와 동일한 팔레트: 단방향=주황 / 양방향=청록
    // 2D: rgba(255,180,50) / rgba(0,230,180)
    const COLOR_ONE_WAY = "#ffb432";
    const COLOR_BIDI = "#00e6b4";

    const makeTubeMat = (isBidi: boolean) =>
      new MeshStandardMaterial({
        color: isBidi ? COLOR_BIDI : COLOR_ONE_WAY,
        emissive: new Color(isBidi ? "#00a87e" : "#b86d00"),
        emissiveIntensity: 0.6,
        metalness: 0.4,
        roughness: 0.25,
        transparent: true,
        opacity: 0.92,
      });

    const makeArrowMat = (isBidi: boolean) =>
      new MeshStandardMaterial({
        color: isBidi ? "#9cffe0" : "#ffd98a",
        emissive: new Color(isBidi ? COLOR_BIDI : COLOR_ONE_WAY),
        emissiveIntensity: 1.1,
        metalness: 0.7,
        roughness: 0.15,
      });

    const makeNodeMat = (isBidi: boolean) =>
      new MeshStandardMaterial({
        color: isBidi ? "#c4ffec" : "#ffe3a8",
        emissive: new Color(isBidi ? COLOR_BIDI : COLOR_ONE_WAY),
        emissiveIntensity: 0.9,
        metalness: 0.6,
        roughness: 0.2,
      });

    // 바닥(floorMesh)이 y=0.15이라 경로·화살표는 그 위에 배치
    const PATH_Y = 0.22;          // 튜브 중심 높이 (바닥 + 여유 + 튜브 반경)
    const TUBE_RADIUS = 0.05;     // 두께감 있는 3D 튜브
    const ARROW_RADIUS = 0.14;    // 뾰족한 화살촉
    const ARROW_HEIGHT = 0.32;    // 뾰족한 길이
    const ARROW_INSET = 0.45;     // 끝점으로부터 안쪽으로 들여서 배치

    navPath.segments.forEach((seg) => {
      const isBidi = seg.direction === "two-way";
      const tubeMat = makeTubeMat(isBidi);
      const arrowMatSeg = makeArrowMat(isBidi);
      const nodeMat = makeNodeMat(isBidi);

      const pts = [seg.from, ...(seg.waypoints || []), seg.to].map((pt) => {
        const p = worldTo3D(pt.x, pt.y, config);
        return new Vector3(p.x, PATH_Y, p.z);
      });

      if (pts.length < 2) return;

      // ── 튜브 (두께감 있는 파이프) ──
      const curve = new CatmullRomCurve3(pts, false, "catmullrom", 0.5);
      const tubeGeo = new TubeGeometry(curve, pts.length * 8, TUBE_RADIUS, 8, false);
      const tubeMesh = new Mesh(tubeGeo, tubeMat);
      tubeMesh.castShadow = true;
      pathGroup.add(tubeMesh);

      // ── 끝점 구 (노드 장식) ──
      const nodeGeo = new SphereGeometry(TUBE_RADIUS * 1.6, 12, 12);
      [pts[0], pts[pts.length - 1]].forEach((p) => {
        const s = new Mesh(nodeGeo, nodeMat);
        s.position.copy(p);
        s.castShadow = true;
        pathGroup.add(s);
      });

      // ── 화살촉: 끝점에서 안쪽으로 들여 배치, 끝쪽을 바라봄 ──
      // "< --------- >" 스타일. 단방향은 to 쪽만, 양방향은 양쪽 다.
      const mkArrow = (base: Vector3, tip: Vector3) => {
        const dir = new Vector3().subVectors(tip, base).normalize();
        const pos = new Vector3().copy(tip).addScaledVector(dir, -ARROW_INSET);
        const cone = new Mesh(
          new ConeGeometry(ARROW_RADIUS, ARROW_HEIGHT, 10),
          arrowMatSeg
        );
        cone.position.copy(pos);
        const up = new Vector3(0, 1, 0);
        cone.quaternion.setFromUnitVectors(up, dir);
        cone.castShadow = true;
        pathGroup.add(cone);
      };

      const first = pts[0];
      const second = pts[1];
      const penultimate = pts[pts.length - 2];
      const last = pts[pts.length - 1];

      // to 방향 화살표 (끝쪽)
      mkArrow(penultimate, last);

      // 양방향이면 from 방향 화살표 (시작쪽, 바깥 향함)
      if (isBidi) {
        mkArrow(second, first);
      }
    });

    ctx.scene.add(pathGroup);
    ctx.pathGroup = pathGroup;
    ctx.needsRender = true;
  }, [navPath, showPath, config]);

  /* ═══ 노드망·이동 경로 (2026-10-19) ═══
   * 노드망은 바닥에 붙은 반투명 띠(실제 폭 0.8m) + 노드 원판 — 배경이라 약하게.
   * 이동 경로는 남은 길만 흰 튜브(파란 빛)로 띄우고, 지나온 길은 가는 회색. 통과 지점 번호는 기둥 위 배지,
   * 목적지는 바닥 링. 2D 와 같은 색 규칙(흰색·파랑)이라 구간 튜브(주황·청록)와 겹치지 않는다. */
  useEffect(() => {
    const ctx = sceneRef.current;
    if (!ctx) return;
    if (ctx.nodeGroup) {
      ctx.scene.remove(ctx.nodeGroup);
      ctx.nodeGroup.traverse((o) => {
        const m = o as Mesh & Sprite;
        if (m.isMesh || m.isSprite) {
          m.geometry?.dispose();
          const mat = m.material as MeshStandardMaterial | SpriteMaterial;
          mat.map?.dispose();
          mat.dispose();
        }
      });
      ctx.nodeGroup = null;
      ctx.needsRender = true;
    }
    const lv = networkLevel === "off" ? null
      : networkLevel === "strong" ? { lane: 0.32, node: 0.95 } : { lane: 0.16, node: 0.6 };
    const hasNet = !!(lv && navNetwork && navNetwork.nodes.length > 0);
    if (!hasNet && !activeRoute) return;

    const LANE_W = 0.8;
    const FLOOR_Y = 0.16;
    const g = new Group();

    if (hasNet && lv && navNetwork) {
      const laneMat = new MeshStandardMaterial({ color: "#dbe6ff", transparent: true, opacity: lv.lane, depthWrite: false, roughness: 0.9 });
      const nodeMat = new MeshStandardMaterial({ color: "#dbe6ff", transparent: true, opacity: lv.node, roughness: 0.6 });
      // 바닥 차로 화살표 — 2D 와 같게 노드망 색·반투명, 0.45 m 간격. 양방향은 두 차로(우측 통행)
      const arrowMat = new MeshStandardMaterial({ color: "#dbe6ff", transparent: true, opacity: Math.min(1, lv.lane + 0.6), depthWrite: false, side: DoubleSide });
      const ARROW_GAP = 0.45;
      const chevrons = (sx: number, sz: number, ex: number, ez: number, half: number) => {
        const L = Math.hypot(ex - sx, ez - sz);
        if (L < 1e-3) return;
        const ux = (ex - sx) / L; const uz = (ez - sz) / L;
        const px = -uz; const pz = ux;
        const n = Math.max(1, Math.floor(L / ARROW_GAP));
        const depth = half * 0.7;
        for (let i = 0; i < n; i++) {
          const t = (i + 0.5) * (L / n);
          const cx = sx + ux * t; const cz = sz + uz * t;
          const tx = cx + ux * depth / 2; const tz = cz + uz * depth / 2;      // 꺾쇠 끝
          for (const side of [1, -1]) {
            const bx = cx - ux * depth / 2 + px * half * side; const bz = cz - uz * depth / 2 + pz * half * side;
            const arm = new PlaneGeometry(Math.hypot(tx - bx, tz - bz), 0.075);
            arm.rotateX(-Math.PI / 2);
            const m = new Mesh(arm, arrowMat);
            m.rotation.y = Math.atan2(-(tz - bz), tx - bx);
            m.position.set((tx + bx) / 2, FLOOR_Y + 0.015, (tz + bz) / 2);
            m.renderOrder = 2;
            g.add(m);
          }
        }
      };
      const byId = new Map(navNetwork.nodes.map((n) => [n.id, n]));
      for (const e of navNetwork.edges) {
        if (e.kind === "excluded") continue;
        const a = byId.get(e.a); const b = byId.get(e.b);
        if (!a || !b) continue;
        const pa = worldTo3D(a.x, a.y, config); const pb = worldTo3D(b.x, b.y, config);
        const dx = pb.x - pa.x; const dz = pb.z - pa.z;
        const geo = new PlaneGeometry(Math.hypot(dx, dz), LANE_W);
        geo.rotateX(-Math.PI / 2);
        const lane = new Mesh(geo, laneMat);
        lane.rotation.y = Math.atan2(-dz, dx);
        lane.position.set((pa.x + pb.x) / 2, FLOOR_Y, (pa.z + pb.z) / 2);
        lane.renderOrder = 1;
        g.add(lane);
        if (e.direction === "forward") {
          chevrons(pa.x, pa.z, pb.x, pb.z, LANE_W * 0.3);              // 한쪽 방향 — 가운데 한 줄 >>>>
        } else {
          // 양방향 — 띠를 두 차로로: 가는 방향 오른쪽 차로 >>>>, 반대 차로 <<<<
          const L = Math.hypot(dx, dz) || 1;
          const ox = (-dz / L) * LANE_W * 0.24; const oz = (dx / L) * LANE_W * 0.24;
          chevrons(pa.x + ox, pa.z + oz, pb.x + ox, pb.z + oz, LANE_W * 0.17);
          chevrons(pb.x - ox, pb.z - oz, pa.x - ox, pa.z - oz, LANE_W * 0.17);
        }
      }
      for (const n of navNetwork.nodes) {
        const p = worldTo3D(n.x, n.y, config);
        const disc = new Mesh(new CylinderGeometry(LANE_W / 2, LANE_W / 2, 0.02, 24), nodeMat);
        disc.position.set(p.x, FLOOR_Y + 0.01, p.z);
        g.add(disc);
      }
    }

    if (activeRoute && activeRoute.points.length > 0) {
      const cur = activeRoute.points.findIndex((p) => p.status === "current");
      const ci = cur >= 0 ? cur : activeRoute.points.length;
      const v = (p: { x: number; y: number }, h: number) => { const q = worldTo3D(p.x, p.y, config); return new Vector3(q.x, h, q.z); };
      const tube = (pts: Vector3[], r: number, mat: MeshStandardMaterial) => {
        for (let i = 0; i < pts.length - 1; i++) {
          if (pts[i].distanceTo(pts[i + 1]) < 1e-3) continue;
          g.add(new Mesh(new TubeGeometry(new LineCurve3(pts[i], pts[i + 1]), 2, r, 10, false), mat));
        }
        for (const p of pts.slice(1, -1)) {      // 꺾이는 곳 이음새
          const j = new Mesh(new SphereGeometry(r, 12, 12), mat);
          j.position.copy(p);
          g.add(j);
        }
      };
      const remain = [activeRoute.robot ?? activeRoute.start, ...activeRoute.points.slice(ci)];
      const passed = [activeRoute.start, ...activeRoute.points.slice(0, ci), ...(activeRoute.robot ? [activeRoute.robot] : [])];
      // 이동 구간 선(튜브)은 그리지 않는다(10-22 사용자) — 2D 와 같이 노드망 바닥 화살표로 길·방향을 보인다
      if (SHOW_ROUTE_TUBE) {
        tube(passed.map((p) => v(p, 0.24)), 0.035, new MeshStandardMaterial({ color: "#9aa3b5", transparent: true, opacity: 0.55 }));
        tube(remain.map((p) => v(p, 0.3)), 0.13, new MeshStandardMaterial({ color: "#1A73E8", transparent: true, opacity: 0.35, depthWrite: false }));
        tube(remain.map((p) => v(p, 0.3)), 0.06, new MeshStandardMaterial({ color: "#ffffff", emissive: new Color("#4da6ff"), emissiveIntensity: 0.5, roughness: 0.3 }));
      }

      const badge = (text: string, done: boolean, cur = false) => {
        const c = document.createElement("canvas");
        c.width = c.height = 64;
        const x = c.getContext("2d")!;
        x.beginPath(); x.arc(32, 32, 26, 0, Math.PI * 2);
        x.fillStyle = done ? "#3B415F" : cur ? "#1A73E8" : "#ffffff"; x.fill();
        x.lineWidth = 6; x.strokeStyle = "#1A73E8"; x.stroke();
        x.fillStyle = done ? "#cfd6e6" : cur ? "#ffffff" : "#1557B0";
        x.font = "bold 30px sans-serif"; x.textAlign = "center"; x.textBaseline = "middle";
        x.fillText(text, 32, 34);
        const s = new Sprite(new SpriteMaterial({ map: new CanvasTexture(c), depthTest: false }));
        s.scale.set(0.5, 0.5, 1);
        s.renderOrder = 10;
        return s;
      };
      const poleMat = new MeshStandardMaterial({ color: "#ffffff", transparent: true, opacity: 0.7 });
      if (SHOW_ROUTE_NODE_BADGES) activeRoute.points.forEach((p, i) => {
        if (!p.node) return;
        const base = v(p, FLOOR_Y);
        const pole = new Mesh(new CylinderGeometry(0.015, 0.015, 0.7, 6), poleMat);
        pole.position.set(base.x, FLOOR_Y + 0.35, base.z);
        g.add(pole);
        const b = badge(p.status === "completed" ? "✓" : String(i + 1), p.status === "completed", p.status === "current");
        // 지금 보낸 묶음 밖(아직 안 보낸 점)은 흐리게
        if (p.status === "pending" && activeRoute.points.some((q) => q.inBundle) && !p.inBundle) {
          (b.material as SpriteMaterial).opacity = 0.45;
          (b.material as SpriteMaterial).transparent = true;
        }
        b.position.set(base.x, FLOOR_Y + 0.85, base.z);
        g.add(b);
      });
      const goal = activeRoute.points[activeRoute.points.length - 1];
      const ringGeo = new RingGeometry(0.42, 0.52, 40);
      ringGeo.rotateX(-Math.PI / 2);
      const ring = new Mesh(ringGeo, new MeshStandardMaterial({ color: "#ffffff", emissive: new Color("#ffffff"), emissiveIntensity: 0.4, side: DoubleSide }));
      const gp = v(goal, FLOOR_Y + 0.02);
      ring.position.copy(gp);
      g.add(ring);
    }

    ctx.scene.add(g);
    ctx.nodeGroup = g;
    ctx.needsRender = true;
  }, [navNetwork, networkLevel, activeRoute, config]);

  return (
    <div ref={containerRef} className={className}
      style={{ width: "100%", height: "100%", position: "relative", background: "#12151f", overflow: "hidden", ...style }}
    />
  );
});

export default Map3DCanvas;

/* ═══ 헬퍼 ═══ */

/**
 * 맵 핀 지오메트리 (납작한 판 + 가운데 구멍)
 * ExtrudeGeometry로 2D Shape를 두께만큼 돌출
 *
 * 이미지 참고: 물방울 실루엣, 가운데 원형 구멍, 광택 있는 판
 */
function createPinGeo(): ExtrudeGeometry {
  const S = 0.22;

  // 통통한 핀 (머리 크게, 테이퍼 짧게)
  const shape = new Shape();
  const headR = 0.7 * S;      // 머리 더 크게
  const headCY = 1.6 * S;     // 머리 위치 낮춰서 전체 비율 통통하게

  shape.moveTo(0, 0);

  // 왼쪽: 넓은 곡선으로 빠르게 머리에 연결
  shape.bezierCurveTo(
    -0.3 * S, 0.4 * S,
    -headR * 1.05, headCY - headR * 0.5,
    -headR, headCY
  );

  // 머리 (반원)
  shape.absarc(0, headCY, headR, Math.PI, 0, true);

  // 오른쪽: 대칭
  shape.bezierCurveTo(
    headR * 1.05, headCY - headR * 0.5,
    0.3 * S, 0.4 * S,
    0, 0
  );

  // 가운데 구멍
  const holeR = 0.32 * S;
  const hole = new Path();
  hole.absarc(0, headCY, holeR, 0, Math.PI * 2, true);
  shape.holes.push(hole);

  // 두께 키움
  const geo = new ExtrudeGeometry(shape, {
    depth: 0.10 * S,
    bevelEnabled: true,
    bevelThickness: 0.04 * S,
    bevelSize: 0.03 * S,
    bevelSegments: 3,
  });

  // 중심 맞추기: Z축(두께)을 중앙으로, 세로(Y)로 서있도록
  // Z축(두께)만 중앙, Y축은 바닥 정렬 (뾰족 끝이 y=0)
  geo.computeBoundingBox();
  const bb = geo.boundingBox!;
  geo.translate(-( bb.min.x + bb.max.x) / 2, -bb.min.y, -(bb.min.z + bb.max.z) / 2);
  return geo;
}

/**
 * 처리된 맵 이미지에서 불투명 영역의 외곽 경계 추출
 * alpha > 0 = 맵 영역, alpha = 0 = 외부
 * 맵 영역 가장자리 픽셀만 1로 마킹
 */
function extractBorderFromImage(canvas: HTMLCanvasElement): { data: Uint8Array; width: number; height: number } {
  const ctx = canvas.getContext("2d")!;
  const W = canvas.width;
  const H = canvas.height;
  const imgData = ctx.getImageData(0, 0, W, H).data;

  // 불투명 마스크 (alpha > 10)
  const opaque = new Uint8Array(W * H);
  for (let i = 0; i < W * H; i++) {
    if (imgData[i * 4 + 3] > 10) opaque[i] = 1;
  }

  // 외곽: 불투명인데 4방향 중 하나라도 투명이면 경계
  const border = new Uint8Array(W * H);
  for (let y = 1; y < H - 1; y++) {
    for (let x = 1; x < W - 1; x++) {
      const idx = y * W + x;
      if (!opaque[idx]) continue;
      if (!opaque[idx - 1] || !opaque[idx + 1] || !opaque[idx - W] || !opaque[idx + W]) {
        border[idx] = 1;
      }
    }
  }

  return { data: border, width: W, height: H };
}

/** 경계 데이터로 벽 InstancedMesh 생성 */
function buildBorderWalls(
  borderMap: { data: Uint8Array; width: number; height: number },
  mapWidthM: number, mapHeightM: number,
): InstancedMesh | null {
  const { data, width: pixW, height: pixH } = borderMap;
  const scaleX = mapWidthM / pixW;
  const scaleZ = mapHeightM / pixH;
  const wallH = 1.0;

  const rects: { px: number; py: number; len: number }[] = [];
  for (let y = 0; y < pixH; y++) {
    let x = 0;
    while (x < pixW) {
      if (data[y * pixW + x] === 1) {
        const sx = x;
        while (x < pixW && data[y * pixW + x] === 1) x++;
        rects.push({ px: sx, py: y, len: x - sx });
      } else { x++; }
    }
  }
  if (rects.length === 0) return null;

  const mesh = new InstancedMesh(
    new BoxGeometry(1, 1, 1),
    new MeshStandardMaterial({
      color: "#5a9ad8", emissive: new Color("#3366aa"), emissiveIntensity: 0.25,
      transparent: true, opacity: 0.75, roughness: 0.3,
    }),
    rects.length
  );

  const dummy = new Object3D();
  for (let i = 0; i < rects.length; i++) {
    const { px, py, len } = rects[i];
    dummy.position.set(
      (px + len / 2) * scaleX - mapWidthM / 2,
      wallH / 2,
      (py + 0.5) * scaleZ - mapHeightM / 2
    );
    dummy.scale.set(len * scaleX, wallH, scaleZ);
    dummy.updateMatrix();
    mesh.setMatrixAt(i, dummy.matrix);
  }
  mesh.instanceMatrix.needsUpdate = true;
  return mesh;
}

function mkLabel(text: string, css: Record<string, string>): HTMLDivElement {
  const el = document.createElement("div");
  el.textContent = text;
  Object.assign(el.style, { position: "absolute", whiteSpace: "nowrap", zIndex: "5", ...css });
  return el;
}

function project(pos: Vector3, cam: PerspectiveCamera, container: HTMLElement, el: HTMLElement, tf: string) {
  const v = pos.clone().project(cam);
  el.style.left = `${(v.x * 0.5 + 0.5) * container.clientWidth}px`;
  el.style.top = `${(-v.y * 0.5 + 0.5) * container.clientHeight}px`;
  el.style.transform = tf;
  el.style.display = v.z > 1 ? "none" : "";
}
