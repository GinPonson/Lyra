import { Ajv, type ValidateFunction } from "ajv";
import addFormats from "ajv-formats";
import type { FinancialToolDefinition } from "../domain/agent/index.js";

const ajv = new Ajv({ strict: false, allErrors: false, coerceTypes: false });
addFormats.default(ajv);
const validators = new WeakMap<FinancialToolDefinition, ValidateFunction>();

/** REST/Genkit 的公开 JSON Schema 同时也是服务器执行前的参数边界。 */
export function isValidFinancialToolArguments(
  definition: FinancialToolDefinition,
  args: unknown,
): args is Readonly<Record<string, unknown>> {
  const validate = validators.get(definition) ?? ajv.compile(definition.inputSchema);
  validators.set(definition, validate);
  return validate(args) === true;
}
