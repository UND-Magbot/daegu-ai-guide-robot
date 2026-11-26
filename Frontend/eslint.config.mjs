import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

// 설치된 eslint-config-next 15 는 옛 설정 양식이라 FlatCompat 로 감싸서 불러온다.
// (Next 16 양식 `import "eslint-config-next/core-web-vitals"` 는 15 에서 읽히지 않는다)
const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    ignores: ["node_modules/**", ".next/**", "out/**", "build/**", "next-env.d.ts"],
  },
];

export default eslintConfig;
