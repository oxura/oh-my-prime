import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const repoRoot = path.resolve(process.argv[2] ?? ".");
const requestedInput = fs.readFileSync(0);
const requested = new Set(
	requestedInput
		.toString("utf8")
		.split("\0")
		.filter(Boolean)
		.map((entry) => path.resolve(repoRoot, entry)),
);

let ts;
try {
	const requireFromProject = createRequire(path.join(repoRoot, "package.json"));
	ts = requireFromProject("typescript");
} catch (error) {
	process.stderr.write(`typescript compiler API unavailable: ${error instanceof Error ? error.message : String(error)}\n`);
	process.exit(42);
}

const configPath = ts.findConfigFile(repoRoot, ts.sys.fileExists, "tsconfig.json");
let compilerOptions = {
	allowJs: true,
	checkJs: false,
	jsx: ts.JsxEmit.Preserve,
	module: ts.ModuleKind.ESNext,
	moduleResolution: ts.ModuleResolutionKind.Bundler,
	target: ts.ScriptTarget.ESNext,
};
if (configPath) {
	const config = ts.readConfigFile(configPath, ts.sys.readFile);
	if (!config.error) {
		compilerOptions = ts.parseJsonConfigFileContent(config.config, ts.sys, path.dirname(configPath)).options;
	}
}

const rootNames = [...requested].filter((filePath) => /\.(?:[cm]?[jt]sx?)$/i.test(filePath));
const program = ts.createProgram({ rootNames, options: compilerOptions });
const checker = program.getTypeChecker();
const emittedSymbols = new Set();
const emittedEdges = new Set();

function relative(filePath) {
	const value = path.relative(repoRoot, filePath).split(path.sep).join("/");
	return value.startsWith("../") ? null : value;
}

function emit(record) {
	process.stdout.write(`${JSON.stringify(record)}\n`);
}

function lineOf(sourceFile, node) {
	return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile, false)).line + 1;
}

function declarationName(node) {
	if (node.name && ts.isIdentifier(node.name)) return node.name.text;
	if (ts.isConstructorDeclaration(node)) return "constructor";
	return null;
}

function declarationKind(node) {
	if (ts.isFunctionDeclaration(node)) return "function";
	if (ts.isClassDeclaration(node)) return "class";
	if (ts.isInterfaceDeclaration(node)) return "interface";
	if (ts.isTypeAliasDeclaration(node)) return "type";
	if (ts.isEnumDeclaration(node)) return "enum";
	if (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) return "method";
	if (ts.isConstructorDeclaration(node)) return "constructor";
	if (ts.isPropertyDeclaration(node) || ts.isPropertySignature(node)) return "property";
	if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) return "variable";
	return null;
}

function qualifiedName(node, name) {
	const names = [name];
	let current = node.parent;
	while (current && !ts.isSourceFile(current)) {
		const parentName = declarationName(current);
		if (parentName) names.unshift(parentName);
		current = current.parent;
	}
	return names.join(".");
}

function symbolKey(node, sourceFile, kind, name) {
	return `${relative(sourceFile.fileName)}:${node.getStart(sourceFile, false)}:${kind}:${name}`;
}

function hasExportModifier(node) {
	const owner = ts.isVariableDeclaration(node) ? node.parent?.parent : node;
	return Boolean(owner?.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
}

function signatureText(node, sourceFile) {
	const body = node.body;
	const end = body ? body.getStart(sourceFile, false) : node.getEnd();
	return sourceFile.text.slice(node.getStart(sourceFile, false), end).trim().slice(0, 2_000);
}

function emitDeclaration(node, sourceFile) {
	const kind = declarationKind(node);
	const name = declarationName(node);
	if (!kind || !name) return null;
	const key = symbolKey(node, sourceFile, kind, name);
	if (!emittedSymbols.has(key)) {
		emittedSymbols.add(key);
		emit({
			type: "symbol",
			key,
			file: relative(sourceFile.fileName),
			kind,
			name,
			qualified_name: qualifiedName(node, name),
			start_line: lineOf(sourceFile, node),
			end_line: sourceFile.getLineAndCharacterOfPosition(node.getEnd()).line + 1,
			exported: hasExportModifier(node),
			signature: signatureText(node, sourceFile),
		});
	}
	return key;
}

function declarationTarget(symbol) {
	if (!symbol) return null;
	let resolved = symbol;
	if (resolved.flags & ts.SymbolFlags.Alias) {
		try {
			resolved = checker.getAliasedSymbol(resolved);
		} catch {
			// Unresolved aliases remain useful as textual targets.
		}
	}
	const declaration = resolved.valueDeclaration ?? resolved.declarations?.[0];
	if (!declaration) return { key: null, name: resolved.getName(), file: null };
	const sourceFile = declaration.getSourceFile();
	const file = relative(sourceFile.fileName);
	const kind = declarationKind(declaration);
	const name = declarationName(declaration) ?? resolved.getName();
	return {
		key: file && kind ? symbolKey(declaration, sourceFile, kind, name) : null,
		name,
		file,
	};
}

function enclosingSymbolKey(node, sourceFile) {
	let current = node.parent;
	while (current && !ts.isSourceFile(current)) {
		const kind = declarationKind(current);
		const name = declarationName(current);
		if (kind && name) return symbolKey(current, sourceFile, kind, name);
		current = current.parent;
	}
	return null;
}

function emitEdge(sourceFile, node, kind, target, confidence = 1) {
	if (!target?.name) return;
	const sourceKey = enclosingSymbolKey(node, sourceFile);
	const record = {
		type: "edge",
		source_key: sourceKey,
		source_file: relative(sourceFile.fileName),
		target_key: target.key ?? null,
		target_name: target.name,
		target_file: target.file ?? null,
		kind,
		line: lineOf(sourceFile, node),
		confidence,
	};
	const key = JSON.stringify(record);
	if (emittedEdges.has(key)) return;
	emittedEdges.add(key);
	emit(record);
}

function isDeclarationIdentifier(node) {
	const parent = node.parent;
	return Boolean(parent?.name === node && declarationKind(parent));
}

for (const sourceFile of program.getSourceFiles()) {
	if (!requested.has(path.resolve(sourceFile.fileName))) continue;
	const file = relative(sourceFile.fileName);
	if (!file) continue;
	const diagnostics = sourceFile.parseDiagnostics ?? [];
	emit({
		type: "diagnostic",
		file,
		parse_status: diagnostics.length ? "error" : "ok",
		errors: diagnostics.map((diagnostic) => ({
			line: sourceFile.getLineAndCharacterOfPosition(diagnostic.start ?? 0).line + 1,
			message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
		})),
	});

	function visit(node) {
		emitDeclaration(node, sourceFile);
		if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
			const specifier = node.moduleSpecifier.text;
			const resolution = ts.resolveModuleName(specifier, sourceFile.fileName, compilerOptions, ts.sys).resolvedModule;
			emitEdge(
				sourceFile,
				node,
				"imports",
				{
					key: null,
					name: specifier,
					file: resolution ? relative(resolution.resolvedFileName) : null,
				},
				resolution ? 1 : 0.7,
			);
		}
		if (ts.isCallExpression(node)) {
			const symbol = checker.getSymbolAtLocation(
				ts.isPropertyAccessExpression(node.expression) ? node.expression.name : node.expression,
			);
			const target = declarationTarget(symbol);
			if (target) {
				emitEdge(sourceFile, node, "calls", target, target.key ? 1 : 0.65);
			} else {
				emitEdge(sourceFile, node, "calls", { key: null, name: node.expression.getText(sourceFile), file: null }, 0.5);
			}
		}
		if ((ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) && node.heritageClauses) {
			for (const clause of node.heritageClauses) {
				const edgeKind = clause.token === ts.SyntaxKind.ImplementsKeyword ? "implements" : "extends";
				for (const typeNode of clause.types) {
					const symbol = checker.getSymbolAtLocation(typeNode.expression);
					emitEdge(sourceFile, typeNode, edgeKind, declarationTarget(symbol), symbol ? 1 : 0.5);
				}
			}
		}
		if (ts.isIdentifier(node) && !isDeclarationIdentifier(node)) {
			const target = declarationTarget(checker.getSymbolAtLocation(node));
			if (target?.key && target.file) emitEdge(sourceFile, node, "references", target, 1);
		}
		ts.forEachChild(node, visit);
	}
	visit(sourceFile);
}
