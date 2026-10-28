// The one place the workbench talks to the server. Screens call these programs, never fetch.
import { KittumApi } from "@kittum/contracts";
import * as Effect from "effect/Effect";
import * as FetchHttpClient from "effect/http/FetchHttpClient";
import * as HttpClient from "effect/http/HttpClient";
import * as HttpClientRequest from "effect/http/HttpClientRequest";

const api = Effect.gen(function* () {
  const client = yield* HttpClient.HttpClient;
  return KittumApi.make(client, {
    transformClient: (c) =>
      Effect.succeed(
        HttpClient.mapRequest(c, HttpClientRequest.prependUrl(window.location.origin)),
      ),
  });
});

export const run = <A, E>(program: (client: KittumApi.KittumApi) => Effect.Effect<A, E>) =>
  Effect.runPromise(api.pipe(Effect.flatMap(program), Effect.provide(FetchHttpClient.layer)));
