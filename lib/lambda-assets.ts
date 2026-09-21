import { existsSync } from 'fs';
import { join } from 'path';

/** Works from ts-node (lib/) and the compiled application (dist/lib/). */
export function lambdaAsset(name: string): string {
  const roots = [join(__dirname, '..'), join(__dirname, '..', '..')];
  const root = roots.find((candidate) => existsSync(join(candidate, 'package.json')) && existsSync(join(candidate, 'lambda')));
  const path = root && join(root, 'lambda', name);
  if (!path || !existsSync(path)) throw new Error(`Lambda source directory not found: ${name}. Keep lambda/ with the CDK application.`);
  return path;
}

export const PYTHON_ASSET_OPTIONS = {
  exclude: ['**/__pycache__', '**/__pycache__/**', '**/*.pyc'],
};
