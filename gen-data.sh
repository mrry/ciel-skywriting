#!/bin/bash
mkdir -p data
echo "Generating SmithWaterman example data"
dd if=/dev/urandom of=data/horizontal_string_random ibs=1000 count=100
dd if=/dev/urandom of=data/vertical_string_random ibs=1000 count=100
echo "Generating KMeans example data"
java -cp `find ext/mahout-0.3/ -iname \*jar -printf %p:`dist/sky-eg-skyhout.jar skywriting.examples.skyhout.kmeans.RandomClusterSequenceFileGenerator data/kmeans-cluster 1 100 100 -1000000 1000000
java -cp `find ext/mahout-0.3/ -iname \*jar -printf %p:`dist/sky-eg-skyhout.jar skywriting.examples.skyhout.kmeans.RandomVectorSequenceFileGenerator data/kmeans-vector 1 10000 100 -1000000 1000000