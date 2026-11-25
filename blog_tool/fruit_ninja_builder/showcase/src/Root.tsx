import { Composition } from "remotion";
import { Showcase, TOTAL_FRAMES } from "./Showcase";

export const Root = () => (
  <Composition id="Showcase" component={Showcase} width={1920} height={1080} fps={30} durationInFrames={TOTAL_FRAMES} />
);
