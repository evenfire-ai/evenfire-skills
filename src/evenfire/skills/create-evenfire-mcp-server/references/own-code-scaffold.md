# Own-code server scaffold

The boilerplate that supports the `src/index.ts` in the skill's §2 and the
`RUN npm run build` + `COPY dist` in the §3 Dockerfile.

## package.json

```json
{
  "name": "mcp-example",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "tsc",
    "dev": "tsx src/index.ts",
    "start": "node dist/index.js"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0",
    "express": "^4.21.0",
    "zod": "^3.23.0"
  },
  "devDependencies": {
    "@types/express": "^4.17.0",
    "@types/node": "^22.0.0",
    "tsx": "^4.19.0",
    "typescript": "^5.6.0"
  }
}
```

`"type": "module"` matches the ESM imports in `src/index.ts`. `dev` runs the
TypeScript directly (no build) for the local smoke test; `build` emits `dist/` for
the image; `start` runs the built server (the Dockerfile's runtime command).

## tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

`outDir: dist` + `rootDir: src` line up with the Dockerfile's `COPY dist` and the
`start` script's `node dist/index.js`.

## registry.json required vs optional (local own-code)

- Required: `name`, `description`, `category`, `transport` (`streamableHttp`),
  `port`, `tools`.
- Optional: `tags`; `serverMode` (defaults to `local`); `version` (defaults from
  `package.json`); `credentialSchema` (omit for a keyless server);
  `egressSummary` (omit when the server makes no outbound calls).
