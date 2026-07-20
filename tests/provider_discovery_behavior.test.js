const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const clientPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "engine-client.js");
const { EngineClient, EngineClientError } = require(clientPath);

function copy(value) {
  return JSON.parse(JSON.stringify(value));
}

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function providerDocument() {
  const capability = { id: "replica.layout.generate", version: 1 };
  return {
    ok: true,
    schema: "librarytool.providers/1",
    providers: [{
      id: "provider.local",
      version: "1.0.0",
      capabilities: [copy(capability)],
      traits: {
        execution: "local",
        network: "offline",
        modes: ["batch"],
        input_media: ["document"],
        output_media: ["layout"],
        input_languages: [],
        output_languages: [],
        limits: {
          max_input_bytes: null,
          max_output_bytes: null,
          max_batch_items: null,
          max_context_tokens: null,
          max_output_tokens: null,
        },
      },
      required_secret_status_ids: ["provider:mistral:api-key"],
      secret_statuses: [{
        id: "provider:mistral:api-key", configured: true,
      }],
      configured: true,
      health: { state: "healthy", reason: null },
      available: true,
    }],
    selections: [{
      capability: copy(capability),
      user_provider_id: "",
      default_provider_id: "provider.local",
      selected_provider_id: "provider.local",
      source: "default",
      command_available: true,
      reason: null,
    }],
    executable_commands: [copy(capability)],
    available_commands: [copy(capability)],
  };
}

function harness(body = providerDocument(), status = 200) {
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(body, status);
    },
  });
  return { client, calls };
}

test("EngineClient exposes strict read-only provider discovery", async () => {
  const body = providerDocument();
  const { client, calls } = harness(body);

  assert.deepEqual(Object.keys(client.providers), ["discover"]);
  assert.equal(Object.isFrozen(client.providers), true);
  assert.equal(await client.providers.discover(), body);
  assert.equal(calls[0].url, "/api/v1/providers");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.cache, "no-cache");
});

test("EngineClient keeps healthy providers closed without an installed command", async () => {
  const body = providerDocument();
  body.executable_commands = [];
  body.available_commands = [];
  body.selections[0].command_available = false;
  body.selections[0].reason = {
    code: "command-not-installed",
    message: "The command implementation is not installed.",
  };
  const { client } = harness(body);

  assert.equal(await client.providers.discover(), body);
});

const malformedCases = {
  "unknown provider fields": (body) => {
    body.providers[0].api_key = "must-not-cross-the-boundary";
  },
  "noncanonical health details": (body) => {
    body.providers[0].health = {
      state: "unavailable",
      reason: {
        code: "runtime-unavailable",
        message: "C:\\private\\runtime.exe failed",
      },
    };
    body.providers[0].available = false;
    body.selections[0].command_available = false;
    body.selections[0].reason = {
      code: "runtime-unavailable",
      message: "C:\\private\\runtime.exe failed",
    };
    body.available_commands = [];
  },
  "secret values": (body) => {
    body.providers[0].required_secret_status_ids = ["provider:local:api-key"];
    body.providers[0].secret_statuses = [{
      id: "provider:local:api-key", configured: true, value: "secret",
    }];
  },
  "secret failure without a missing secret": (body) => {
    body.providers[0].configured = false;
    body.providers[0].available = false;
    body.providers[0].health = {
      state: "unavailable",
      reason: {
        code: "secret-unavailable",
        message: "A required credential is not configured.",
      },
    };
    body.selections[0].command_available = false;
    body.selections[0].reason = copy(body.providers[0].health.reason);
    body.available_commands = [];
  },
  "implicit provider fallback": (body) => {
    body.selections[0].user_provider_id = "provider.missing";
  },
  "ambiguous unconfigured health": (body) => {
    body.providers[0].configured = false;
    body.providers[0].available = false;
    body.selections[0].command_available = false;
    body.selections[0].reason = {
      code: "provider-unavailable",
      message: "The selected provider is unavailable.",
    };
    body.available_commands = [];
  },
  "degraded provider with an unavailable reason": (body) => {
    body.providers[0].health = {
      state: "degraded",
      reason: {
        code: "disabled",
        message: "The provider is disabled.",
      },
    };
  },
  "inconsistent selection reasons": (body) => {
    body.selections[0].default_provider_id = "";
    body.selections[0].selected_provider_id = "";
    body.selections[0].source = "none";
    body.selections[0].command_available = false;
    body.selections[0].reason = {
      code: "disabled",
      message: "The provider is disabled.",
    };
    body.available_commands = [];
  },
  "missing capability selection": (body) => {
    body.selections = [];
    body.available_commands = [];
  },
  "unsupported schema": (body) => {
    body.schema = "librarytool.providers/2";
  },
  "inconsistent available commands": (body) => {
    body.available_commands = [];
  },
  "unselected executable commands": (body) => {
    body.executable_commands.push({
      id: "translation.layer.generate", version: 1,
    });
  },
  "available but nonexecutable commands": (body) => {
    body.executable_commands = [];
    body.selections[0].command_available = false;
    body.selections[0].reason = {
      code: "command-not-installed",
      message: "The command implementation is not installed.",
    };
  },
  "command fingerprints": (body) => {
    body.providers[0].traits.command_sha256 = "private-command-hash";
  },
};

for (const [name, mutate] of Object.entries(malformedCases)) {
  test(`provider discovery rejects ${name}`, async () => {
    const body = copy(providerDocument());
    mutate(body);
    const { client } = harness(body);

    await assert.rejects(client.providers.discover(), (error) => {
      assert.equal(error instanceof EngineClientError, true);
      assert.equal(error.code, "invalid-response");
      assert.equal(error.status, 200);
      assert.equal(error.url, "/api/v1/providers");
      return true;
    });
  });
}
