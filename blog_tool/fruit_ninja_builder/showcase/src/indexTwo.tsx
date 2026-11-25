import React from "react";
import { Composition, registerRoot } from "remotion";
import { ShowcaseTwo, TOTAL_FRAMES_TWO } from "./ShowcaseTwo";

registerRoot(() => <Composition id="ShowcaseTwo" component={ShowcaseTwo} width={1920} height={1080} fps={30} durationInFrames={TOTAL_FRAMES_TWO} />);
