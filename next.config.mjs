/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  turbopack: {},
  webpack: (config) => {
    // pdfjs-dist uses canvas which is not available in Node — ignore it
    config.resolve.alias.canvas = false;
    return config;
  },
};

export default nextConfig;
