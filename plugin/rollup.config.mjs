import deckyPlugin from '@decky/rollup';
import { readFileSync, writeFileSync, rmSync, existsSync } from 'fs';
import { join } from 'path';

// 插件版本号从 package.json 注入，避免界面里再手抄一份（抄了就会和主程序对不上）
const pkg = JSON.parse(readFileSync('./package.json', 'utf-8'));

/**
 * 发布产物不带 sourcemap。
 *
 * @decky/rollup 默认开 sourcemap，构建产物会多一个 map 文件。
 * 正式打包不需要它，`SM=1 pnpm build` 时才保留，方便看栈帧。
 */
const dropSourcemap = () => ({
  name: 'drop-sourcemap',
  writeBundle(options) {
    if (process.env.SM) return;
    const dir = options.dir || 'dist';
    for (const f of ['index.js.map', 'index.js.map.json']) {
      const p = join(dir, f);
      if (existsSync(p)) rmSync(p);
    }
    const js = join(dir, 'index.js');
    if (existsSync(js)) {
      const code = readFileSync(js, 'utf-8').replace(/\n?\/\/# sourceMappingURL=.*$/m, '');
      writeFileSync(js, code);
    }
  },
});

export default deckyPlugin({
  plugins: [
    {
      name: 'plugin-version',
      transform(code, id) {
        if (!id.includes('/src/') && !id.includes('\\src\\')) return null;
        return { code: code.replace(/__PLUGIN_VERSION__/g, JSON.stringify(pkg.version)), map: null };
      },
    },
    dropSourcemap(),
  ],
});
