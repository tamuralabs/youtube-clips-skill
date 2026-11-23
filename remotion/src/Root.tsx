import React from "react";
import { CalculateMetadataFunction, Composition } from "remotion";
import { Clip, ClipProps } from "./Clip";

const calculateMetadata: CalculateMetadataFunction<ClipProps> = ({ props }) => ({
  durationInFrames: props.durationInFrames,
});

const exemplo: ClipProps = {
  src: "clip.mp4",
  durationInFrames: 300,
  srcWidth: 1920,
  srcHeight: 1080,
  hook: "Gancho do banner de abertura",
  captions: [],
  track: [],
  logo: null,
  style: { font: "Montserrat-ExtraBold.ttf", fontWeight: 800, text: "#FFFFFF", highlight: "#FFD600", hookBg: "#D7141A", hookText: "#FFFFFF" },
  cta: null,
  end: null,
};

export const RemotionRoot: React.FC = () => (
  <Composition id="Clip" component={Clip} durationInFrames={300} fps={30} width={1080} height={1920} defaultProps={exemplo} calculateMetadata={calculateMetadata} />
);
